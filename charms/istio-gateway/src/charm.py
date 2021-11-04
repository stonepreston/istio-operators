#!/usr/bin/env python3

import json
import logging
import subprocess
from functools import wraps

from jinja2 import Environment, FileSystemLoader
from lightkube import ApiError, Client
from lightkube.resources.core_v1 import ConfigMap
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from serialized_data_interface import NoVersionsListed, get_interfaces


def update_status(f):
    """Updates status after decorated event handler is run.

    WARNING: For demonstration purposes only. Void where prohibited by law.

    TODO:
     - Move out to separate repo
     - Implement memory-only code for unit testing
     - Clean up code
    """
    @wraps(f)
    def wrapper(self, event):
        try:
            status = f(self, event)
        except Exception as err:
            status = self.app, BlockedStatus(str(err))

        client = Client()
        cm_name = self.model.app.name + '-status'
        try:
            configmap = client.get(ConfigMap, cm_name, namespace=self.model.name)
        except ApiError as err:
            if err.response.status_code == 404:
                configmap = ConfigMap.from_dict({"metadata": {"name": cm_name, "namespace": self.model.name}, "data": {}})
                client.create(configmap)
            else:
                raise

        if configmap.data is None:
            configmap.data = {}

        if status is None:
            if f.__name__ in configmap.data:
                del configmap.data[f.__name__]
        else:
            scope, status = status
            configmap.data[f.__name__] = json.dumps([type(scope).__name__, type(status).__name__, status.message])

        client.replace(configmap)

        statuses = [json.loads(st) for st in configmap.data.values()]
        app_statuses = [
            (kind, message) for scope, kind, message in statuses if scope == 'Application'
        ]
        unit_statuses = [
            (kind, message) for scope, kind, message in statuses if scope == 'Unit'
        ]
        rel_statuses = [
            (kind, message) for scope, kind, message in statuses if scope == 'Relation'
        ]

        app_statuses += rel_statuses

        if app_statuses:
            app_status_type = eval(app_statuses[0][0])
            self.model.app.status = app_status_type('; '.join([st[1] for st in app_statuses]))
        elif self.model.unit.is_leader():
            self.model.app.status = ActiveStatus('')

        if unit_statuses:
            unit_status_type = eval(unit_statuses[0][0])
            self.model.unit.status = unit_status_type(
                '; '.join([st[1] for st in unit_statuses])
            )
        else:
            self.model.unit.status = ActiveStatus('')

    return wrapper


class Operator(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)

        self.log = logging.getLogger(__name__)

        self.framework.observe(self.on.install, self.install)
        self.framework.observe(self.on["istio-pilot"].relation_changed, self.install)
        self.framework.observe(self.on.config_changed, self.install)
        self.framework.observe(self.on.remove, self.remove)

    @update_status
    def install(self, event):
        """Install charm."""

        if not self.unit.is_leader():
            return self.unit, WaitingStatus("Waiting for leadership")

        try:
            self.interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            return self.app, WaitingStatus(str(err))

        if self.model.config['kind'] not in ('ingress', 'egress'):
            return self.app, BlockedStatus('Config item `kind` must be set')

        if not self.model.relations['istio-pilot']:
            return self.app, BlockedStatus("Waiting for istio-pilot relation")

        if not ((pilot := self.interfaces["istio-pilot"]) and pilot.get_data()):
            return self.app, BlockedStatus("Waiting for istio-pilot relation data")

        pilot = list(pilot.get_data().values())[0]

        env = Environment(loader=FileSystemLoader('src'))
        template = env.get_template('manifest.yaml')
        rendered = template.render(
            kind=self.model.config['kind'],
            namespace=self.model.name,
            pilot_host=pilot['service-name'],
            pilot_port=pilot['service-port'],
        )

        subprocess.run(["./kubectl", "apply", "-f-"], input=rendered.encode('utf-8'), check=True)

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

        subprocess.run(
            ["./kubectl", "delete", "-f-"],
            input=rendered.encode('utf-8'),
            # Can't remove stuff yet: https://bugs.launchpad.net/juju/+bug/1941655
            # check=True
        )


if __name__ == "__main__":
    main(Operator)
