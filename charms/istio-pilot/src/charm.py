#!/usr/bin/env python3

import logging
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces
from lightkube import Client, codecs
from lightkube.core.exceptions import LoadResourceError, ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Service


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            self.model.unit.status = WaitingStatus("Waiting for leadership")
            return

        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            self.model.unit.status = WaitingStatus(str(err))
            return
        except NoCompatibleVersions as err:
            self.model.unit.status = BlockedStatus(str(err))
            return
        else:
            self.model.unit.status = ActiveStatus()

        self.log = logging.getLogger(__name__)

        # Every lightkube API call will use the model name as the namespace by default
        self.lightkube_client = Client(namespace=self.model.name, field_manager="lightkube")
        # Create namespaced resource classes for lightkube client
        self.envoy_filter_resource = create_namespaced_resource(group="networking.istio.io",
                                                                version="v1alpha3",
                                                                kind="EnvoyFilter",
                                                                plural="envoyfilters",
                                                                verbs=None)

        self.virtual_service_resource = create_namespaced_resource(group="networking.istio.io",
                                                                   version="v1alpha3",
                                                                   kind="VirtualService",
                                                                   plural="virtualservices",
                                                                   verbs=None)

        self.destination_rule_resource = create_namespaced_resource(group="networking.istio.io",
                                                                    version="v1alpha3",
                                                                    kind="DestinationRule",
                                                                    plural="destinationrules",
                                                                    verbs=None)

        self.gateway_resource = create_namespaced_resource(group="networking.istio.io",
                                                           version="v1beta1",
                                                           kind="Gateway",
                                                           plural="gateways",
                                                           verbs=None)

        self.rbac_config_resource = create_namespaced_resource(group="rbac.istio.io",
                                                               version="v1alpha1",
                                                               kind="RbacConfig",
                                                               plural="rbacconfigs",
                                                               verbs=None)

        self.env = Environment(loader=FileSystemLoader('src'))

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on.remove, self.remove)

        self.framework.observe(self.on.config_changed, self.handle_default_gateways)

        self.framework.observe(self.on["istio-pilot"].relation_changed, self.send_info)

        self.framework.observe(self.on['ingress'].relation_changed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_departed, self.handle_ingress)
        self.framework.observe(self.on['ingress'].relation_broken, self.handle_ingress)
        self.framework.observe(self.on['ingress-auth'].relation_changed, self.handle_ingress_auth)
        self.framework.observe(self.on['ingress-auth'].relation_departed, self.handle_ingress_auth)

    def install(self, event):
        """Install charm."""

        subprocess.check_call(
            [
                "./istioctl",
                "install",
                "-y",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        manifests = subprocess.check_output(
            [
                "./istioctl",
                "manifest",
                "generate",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={self.model.name}",
            ]
        )

        # Todo: Test if unauthorized stuff is even needed with light kube
        try:
            resources = [self.virtual_service_resource, self.destination_rule_resource, self.gateway_resource,
                         self.envoy_filter_resource, self.rbac_config_resource]
            for resource in resources:
                self._delete_resource(resource)

            self._delete_manifest(manifests, ignore_not_found=True)
        except ApiError as err:
            if "(Unauthorized)" in err.status.message:
                # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
                pass
            else:
                self.log.error(str(err))
                raise

    def handle_default_gateways(self, event):
        t = self.env.get_template('gateway.yaml.j2')
        gateways = self.model.config['default-gateways'].split(',')
        manifest = ''.join(t.render(name=g) for g in gateways)

        self._delete_resource(self.gateway_resource)
        self._apply_manifest(manifest)

    def send_info(self, event):
        if self.interfaces["istio-pilot"]:
            self.interfaces["istio-pilot"].send_data(
                {
                    "service-name": f'istiod.{self.model.name}.svc',
                    "service-port": '15012',
                }
            )

    def handle_ingress(self, event):
        gateway_address = self._get_gateway_address()
        if not gateway_address:
            self.unit.status = WaitingStatus("Waiting for gateway address")
            event.defer()
            return
        else:
            self.unit.status = ActiveStatus()

        ingress = self.interfaces['ingress']
        if ingress:
            # Filter out data we sent back.
            routes = {
                (rel, app): route
                for (rel, app), route in sorted(
                    ingress.get_data().items(), key=lambda tup: tup[0][0].id
                )
                if app != self.app
            }
        else:
            routes = {}

        if isinstance(event, RelationBrokenEvent):
            # The app-level data is still visible on a broken relation, but we
            # shouldn't be keeping the VirtualService for that related app.
            del routes[(event.relation, event.app)]

        t = self.env.get_template('virtual_service.yaml.j2')
        gateway = self.model.config['default-gateways'].split(',')[0]

        def get_kwargs(rel, version, route):
            """Handles both v1 and v2 ingress relations.

            v1 ingress schema doesn't allow sending over a namespace.
            """
            kwargs = {'gateway': gateway, **route}

            if 'namespace' not in kwargs:
                kwargs['namespace'] = self.model.name

            prefix = kwargs["prefix"]
            kwargs.setdefault("rewrite", prefix)
            if prefix == "/":
                kwargs["unit_prefix"] = "/unit-{}/"
            elif prefix.endswith("/"):
                kwargs["unit_prefix"] = prefix[:-1] + "-unit-{}/"
            else:
                kwargs["unit_prefix"] = prefix + "-unit-{}"

            kwargs["units"] = sorted(rel.units, key=lambda u: u.name)

            return kwargs

        vses = [
            t.render(**get_kwargs(rel, ingress.versions[app.name], route))
            for ((rel, app), route) in routes.items()
        ]
        virtual_services = ''.join(vses)

        resources = [self.virtual_service_resource, self.destination_rule_resource]
        for resource in resources:
            self._delete_resource(resource)

        if routes:
            self._apply_manifest(virtual_services)

        # Send URL(s) back
        for (rel, app), route in routes.items():
            if int(ingress.versions[app.name][1:]) < 3:
                # only version 3+ supports response data
                continue
            prefix = route["prefix"].strip("/")
            response_data = {
                "url": f"http://{gateway_address}/{prefix}/",
            }
            if route.get("per_unit_routes", False):
                unit_urls = response_data["unit_urls"] = {}
                for unit in rel.units:
                    unit_num = unit.name.split("/")[-1]
                    unit_path = f"{prefix}-unit-{unit_num}"
                    unit_urls[unit.name] = f"http://{gateway_address}/{unit_path}/"
            ingress.send_data(response_data, app_name=app.name)

    def handle_ingress_auth(self, event):
        auth_routes = self.interfaces['ingress-auth']
        if auth_routes:
            auth_routes = list(auth_routes.get_data().values())
        else:
            auth_routes = []

        if not all(ar.get("service") for ar in auth_routes):
            self.model.unit.status = WaitingStatus("Waiting for auth route connection information.")
            return

        rbac_configs = Path('src/rbac_config.yaml').read_text() if auth_routes else None

        t = self.env.get_template('auth_filter.yaml.j2')
        auth_filters = ''.join(
            t.render(
                namespace=self.model.name,
                **{
                    'request_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-request-headers', [])],
                        default_flow_style=True,
                    ),
                    'response_headers': yaml.safe_dump(
                        [{'exact': h} for h in r.get('allowed-response-headers', [])],
                        default_flow_style=True,
                    ),
                    'port': r['port'],
                    'service': r['service'],
                },
            )
            for r in auth_routes
        )

        manifests = [rbac_configs, auth_filters]
        manifests = '\n'.join([m for m in manifests if m])

        resources = [self.envoy_filter_resource, self.rbac_config_resource]
        for resource in resources:
            self._delete_resource(resource)

        self._apply_manifest(manifests)

    def _get_gateway_address(self):
        """Look up the load balancer address for the ingress gateway.

        If the gateway isn't available or doesn't have a load balancer address yet,
        returns None.
        """
        services = self.lightkube_client.list(Service, labels={"istio": "ingressgateway"}, namespace=self.model.name)
        for service in services:
            ingress_points = service.status.loadBalancer.ingress
            if ingress_points:
                return ingress_points[0].ip
        return None

    # Todo: Add tests for exceptions
    def _delete_resource(self, resource):
        for obj in self.lightkube_client.list(resource, labels={"app.juju.is/created-by": f"{self.app.name}"}):
            try:
                self.lightkube_client.delete(resource, obj.metadata.name)
            except ApiError as err:
                self.log.error(err.status.message)
                raise

    # Todo: Add tests for exceptions
    def _apply_manifest(self, manifest):
        try:
            for obj in codecs.load_all_yaml(manifest):
                self.lightkube_client.apply(obj)
        except LoadResourceError as err:
            self.log.error(f"Error encountered while attempting to create Lightkube resources from YAML. {str(err)}")
            raise

    # Todo: Add tests for exceptions
    def _delete_manifest(self, manifest, ignore_not_found=False):
        try:
            for obj in codecs.load_all_yaml(manifest):
                try:
                    self.lightkube_client.delete(obj.__class__, obj.metadata.name)
                except ApiError as err:
                    if "not found" in err.status.message and ignore_not_found:
                        pass
                    else:
                        self.log.error(err.status.message)
                        raise
        except LoadResourceError as err:
            self.log.error(f"Error encountered while attempting to create Lightkube resources from YAML. {str(err)}")
            raise


if __name__ == "__main__":
    main(Operator)
