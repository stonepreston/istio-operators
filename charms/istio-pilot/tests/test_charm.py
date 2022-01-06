from unittest.mock import call as Call

import lightkube
import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


@pytest.fixture()
def mocked_list(mocked_client, mocker):
    # When looking up services, list needs to return a subscriptable (MagicMock mocks are subscriptable) object
    # that has an IP attribute equal to 127.0.0.1
    mocked_ingress = mocker.MagicMock()
    mocked_ingress.ip = "127.0.0.1"
    mocked_service_obj = mocker.MagicMock()
    mocked_service_obj.status.loadBalancer.ingress.__getitem__.return_value = mocked_ingress
    # Otherwise, list needs to return a list of at least one object so that delete gets called
    mocked_resource_obj = mocker.Mock()

    def my_side_effect(*args, **kwargs):
        if args[0].__name__ == "Service":
            return [mocked_service_obj]
        else:
            # List needs to return a list of at least one object so that delete gets called
            return [mocked_resource_obj]

    mocked_client.return_value.list.side_effect = my_side_effect


# autouse to ensure we don't accidentally call out, but
# can also be used explicitly to get access to the mock.
@pytest.fixture(autouse=True)
def subprocess(mocker):
    subprocess = mocker.patch("charm.subprocess")
    for method_name in ("run", "call", "check_call", "check_output"):
        method = getattr(subprocess, method_name)
        method.return_value.returncode = 0
        method.return_value.stdout = b""
        method.return_value.stderr = b""
        method.return_value.output = b""
        mocker.patch(f"subprocess.{method_name}", method)
    yield subprocess


@pytest.fixture
def harness():
    return Harness(Operator)


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_basic(harness, subprocess):
    check_call = subprocess.check_call
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    expected_args = [
        './istioctl',
        'install',
        '-y',
        '-s',
        'profile=minimal',
        '-s',
        'values.global.istioNamespace=None',
    ]

    assert len(check_call.call_args_list) == 1
    assert check_call.call_args_list[0].args == (expected_args,)
    assert check_call.call_args_list[0].kwargs == {}

    assert harness.charm.model.unit.status == ActiveStatus('')


def test_default_gateways(harness, subprocess):
    {
        'apiVersion': 'networking.istio.io/v1beta1',
        'kind': 'Gateway',
        'metadata': {'name': 'istio-gateway'},
        'spec': {
            'selector': {'istio': 'ingressgateway'},
            'servers': [
                {'hosts': ['*'], 'port': {'name': 'http', 'number': 80, 'protocol': 'HTTP'}}
            ],
        },
    },


def get_unique_calls(call_args_list):
    uniques = []
    for call in call_args_list:
        if call in uniques:
            continue
        else:
            uniques.append(call)

    return uniques


def test_with_ingress_relation(harness, subprocess, mocked_client, mocked_list):
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("ingress", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service": "service-name", "port": 6666, "prefix": "/"}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    harness.begin_with_initial_hooks()

    harness.framework.reemit()

    expected = {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'service-name'},
            'spec': {
                'gateways': ['None/istio-gateway'],
                'hosts': ['*'],
                'http': [
                    {
                        'name': 'app-route',
                        'match': [{'uri': {'prefix': '/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.None.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    }
                ],
            },
        }

    assert check_call.call_args_list == [
        Call(
            [
                './istioctl',
                'install',
                '-y',
                '-s',
                'profile=minimal',
                '-s',
                'values.global.istioNamespace=None',
            ]
        )
    ]

    delete_calls = get_unique_calls(mocked_client.return_value.delete.call_args_list)
    deleted_resource_types = []
    # Skip the first unique delete call since that is the call for the gateway
    for call in delete_calls[1:]:
        resource_type = call[0][0]
        deleted_resource_types.append(resource_type.__name__)
    assert deleted_resource_types == ['VirtualService', 'DestinationRule']

    # Skip the first unique apply call since that is the call for the gateway
    apply_calls = get_unique_calls(mocked_client.return_value.apply.call_args_list[1:])
    assert apply_calls[0][0][0] == expected


def test_with_ingress_relation_v3(harness, subprocess, mocked_client, mocked_list):
    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )

    rel_id = harness.add_relation("ingress", "app")
    harness.add_relation_unit(rel_id, "app/0")
    harness.add_relation_unit(rel_id, "app/1")
    data = {
        "service": "service-name",
        "port": 6666,
        "prefix": "/app/",
        "rewrite": "/",
        "namespace": "ns",
        "per_unit_routes": True,
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v3", "data": yaml.dump(data)},
    )

    rel_id2 = harness.add_relation("ingress", "app2")
    harness.add_relation_unit(rel_id2, "app2/0")
    harness.add_relation_unit(rel_id2, "app2/1")
    data2 = {
        "service": "app2",
        "port": 6666,
        "prefix": "/app2/",
        "rewrite": "/",
        "namespace": "ns",
        "per_unit_routes": False,
    }
    harness.update_relation_data(
        rel_id2,
        "app2",
        {"_supported_versions": "- v3", "data": yaml.dump(data2)},
    )

    try:
        harness.begin_with_initial_hooks()
    except KeyError as e:
        if str(e) == "'v3'":
            pytest.xfail("Schema v3 not merged yet")
        raise

    expected_input = [
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'service-name'},
            'spec': {
                'gateways': ['ns/istio-gateway'],
                'hosts': ['*'],
                'http': [
                    {
                        'name': 'app-route',
                        'match': [{'uri': {'prefix': '/app/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.ns.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    },
                    {
                        'name': 'unit-0-route',
                        'match': [{'uri': {'prefix': '/app-unit-0/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.ns.svc.cluster.local',
                                    'port': {'number': 6666},
                                    'subset': 'app-0',
                                }
                            }
                        ],
                    },
                    {
                        'name': 'unit-1-route',
                        'match': [{'uri': {'prefix': '/app-unit-1/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'service-name.ns.svc.cluster.local',
                                    'port': {'number': 6666},
                                    'subset': 'app-1',
                                }
                            }
                        ],
                    },
                ],
            },
        },
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'DestinationRule',
            'metadata': {'name': 'service-name'},
            'spec': {
                'host': 'service-name.ns.svc.cluster.local',
                'subsets': [
                    {'labels': {'statefulset.kubernetes.io/pod-name': 'app-0'}, 'name': 'app-0'},
                    {'labels': {'statefulset.kubernetes.io/pod-name': 'app-1'}, 'name': 'app-1'},
                ],
            },
        },
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'app2'},
            'spec': {
                'gateways': ['ns/istio-gateway'],
                'hosts': ['*'],
                'http': [
                    {
                        'name': 'app-route',
                        'match': [{'uri': {'prefix': '/app2/'}}],
                        'rewrite': {'uri': '/'},
                        'route': [
                            {
                                'destination': {
                                    'host': 'app2.ns.svc.cluster.local',
                                    'port': {'number': 6666},
                                }
                            }
                        ],
                    },
                ],
            },
        },
    ]

    apply_calls = get_unique_calls(mocked_client.return_value.apply.call_args_list)
    apply_args = []
    # Skip the first unique call since that is the call for the gateway
    for call in apply_calls[1:]:
        apply_args.append(call[0][0])
    assert apply_args == expected_input

    sent_data = harness.get_relation_data(rel_id, harness.charm.app.name)
    assert "data" in sent_data
    sent_data = yaml.safe_load(sent_data["data"])
    assert sent_data == {
        "url": "http://127.0.0.1/app/",
        "unit_urls": {
            "app/0": "http://127.0.0.1/app-unit-0/",
            "app/1": "http://127.0.0.1/app-unit-1/",
        },
    }


def test_with_ingress_auth_relation(harness, subprocess, mocked_client, mocked_list):
    check_call = subprocess.check_call

    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("ingress-auth", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {
        "service": "service-name",
        "port": 6666,
        "allowed-request-headers": ['foo'],
        "allowed-response-headers": ['bar'],
    }
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )
    harness.begin_with_initial_hooks()

    expected = [
        {
            'apiVersion': 'rbac.istio.io/v1alpha1',
            'kind': 'RbacConfig',
            'metadata': {'name': 'default'},
            'spec': {'mode': 'OFF'},
        },
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'EnvoyFilter',
            'metadata': {'name': 'authn-filter'},
            'spec': {
                'filters': [
                    {
                        'filterConfig': {
                            'httpService': {
                                'authorizationRequest': {
                                    'allowedHeaders': {'patterns': [{'exact': 'foo'}]}
                                },
                                'authorizationResponse': {
                                    'allowedUpstreamHeaders': {'patterns': [{'exact': 'bar'}]}
                                },
                                'serverUri': {
                                    'cluster': 'outbound|6666||service-name.None.svc.cluster.local',
                                    'failureModeAllow': False,
                                    'timeout': '10s',
                                    'uri': 'http://service-name.None.svc.cluster.local:6666',
                                },
                            }
                        },
                        'filterName': 'envoy.ext_authz',
                        'filterType': 'HTTP',
                        'insertPosition': {'index': 'FIRST'},
                        'listenerMatch': {'listenerType': 'GATEWAY'},
                    }
                ],
                'workloadLabels': {'istio': 'ingressgateway'},
            },
        },
    ]

    assert check_call.call_args_list == [
        Call(
            [
                './istioctl',
                'install',
                '-y',
                '-s',
                'profile=minimal',
                '-s',
                'values.global.istioNamespace=None',
            ]
        )
    ]

    delete_calls = get_unique_calls(mocked_client.return_value.delete.call_args_list)
    deleted_resource_types = []
    # Skip the first unique call since that is the call for the gateway
    for call in delete_calls[1:]:
        resource_type = call[0][0]
        deleted_resource_types.append(resource_type.__name__)
    assert deleted_resource_types == ['EnvoyFilter', 'RbacConfig']

    apply_calls = get_unique_calls(mocked_client.return_value.apply.call_args_list)
    apply_args = []
    # Skip the first unique call since that is the call for the gateway
    for call in apply_calls[1:]:
        apply_args.append(call[0][0])
    assert apply_args == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)
