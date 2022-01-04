#!/usr/bin/env python3

import logging
import subprocess

from jinja2 import Environment, FileSystemLoader
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoCompatibleVersions, NoVersionsListed, get_interfaces
from lightkube import Client, codecs
from lightkube.core.exceptions import LoadResourceError


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

        self.lightkube_client = Client(field_manager="lightkube")

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.install)
        self.framework.observe(self.on.config_changed, self.install)
        self.framework.observe(self.on.remove, self.remove)

    def install(self, event):
        """Install charm."""

        if self.model.config['kind'] not in ('ingress', 'egress'):
            self.model.unit.status = BlockedStatus('Config item `kind` must be set')
            return

        if not self.model.relations['istio-pilot']:
            self.model.unit.status = BlockedStatus("Waiting for istio-pilot relation")
            return

        if not ((pilot := self.interfaces["istio-pilot"]) and pilot.get_data()):
            self.model.unit.status = WaitingStatus("Waiting for istio-pilot relation data")
            return

        pilot = list(pilot.get_data().values())[0]

        env = Environment(loader=FileSystemLoader('src'))
        template = env.get_template('manifest.yaml')
        rendered = template.render(
            kind=self.model.config['kind'],
            namespace=self.model.name,
            pilot_host=pilot['service-name'],
            pilot_port=pilot['service-port'],
        )

        # subprocess.run(["./kubectl", "apply", "-f-"], input=rendered.encode('utf-8'), check=True)
        try:
            for obj in codecs.load_all_yaml(rendered):
                self.lightkube_client.apply(obj, namespace=obj.metadata.namespace)
        except LoadResourceError as err:
            self.model.unit.status = BlockedStatus(str(err))
            return

        self.unit.status = ActiveStatus()

    def remove(self, event):
        """Remove charm."""

        env = Environment(loader=FileSystemLoader('src'))
        template = env.get_template('manifest.yaml')
        rendered = template.render(
            kind=self.model.config['kind'],
            namespace=self.model.name,
            pilot_host='foo',
            pilot_port='foo',
        )

        # try:
        #     subprocess.run(
        #         ["./kubectl", "delete", "-f-"],
        #         input=rendered.encode('utf-8'),
        #         capture_output=True,
        #         check=True,
        #     )
        # except subprocess.CalledProcessError as e:
        #     if "(Unauthorized)" in e.stderr.decode("utf-8"):
        #         # Ignore error from https://bugs.launchpad.net/juju/+bug/1941655
        #         pass
        #     else:
        #         # But surface any other errors
        #         self.log.error(e.stderr)
        #         raise
        try:
            for obj in codecs.load_all_yaml(rendered):
                # Todo: Ignore unauthorized error, and log others when deleting
                self.lightkube_client.delete(obj.__class__, obj.metadata.name, namespace=obj.metadata.namespace)
        except LoadResourceError as err:
            self.model.unit.status = BlockedStatus(str(err))
            return


if __name__ == "__main__":
    main(Operator)
