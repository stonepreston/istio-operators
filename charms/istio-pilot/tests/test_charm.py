from unittest.mock import call as Call

import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, WaitingStatus
from ops.testing import Harness
from lightkube.core.exceptions import LoadResourceError, ApiError


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


def test_with_ingress_relation(harness, subprocess, get_unique_calls, get_deleted_resource_types, mocked_client):
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
                'gateways': ['istio-gateway'],
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
    assert get_deleted_resource_types(delete_calls[1:]) == ['VirtualService', 'DestinationRule']

    # Skip the first unique apply call since that is the call for the gateway
    apply_calls = get_unique_calls(mocked_client.return_value.apply.call_args_list[1:])
    assert apply_calls[0][0][0] == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)

    harness.remove_relation(rel_id)
    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_with_ingress_relation_v3(harness, subprocess, get_unique_calls, mocked_client):
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
                'gateways': ['istio-gateway'],
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
                                    'subset': 'service-name-0',
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
                                    'subset': 'service-name-1',
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
                    {
                        'labels': {'statefulset.kubernetes.io/pod-name': 'service-name-0'},
                        'name': 'service-name-0',
                    },
                    {
                        'labels': {'statefulset.kubernetes.io/pod-name': 'service-name-1'},
                        'name': 'service-name-1',
                    },
                ],
            },
        },
        {
            'apiVersion': 'networking.istio.io/v1alpha3',
            'kind': 'VirtualService',
            'metadata': {'name': 'app2'},
            'spec': {
                'gateways': ['istio-gateway'],
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


def test_with_ingress_auth_relation(harness, subprocess, get_unique_calls, get_deleted_resource_types, mocked_client):
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
    assert get_deleted_resource_types(delete_calls[1:]) == ['EnvoyFilter', 'RbacConfig']

    apply_calls = get_unique_calls(mocked_client.return_value.apply.call_args_list)
    apply_args = []
    # Skip the first unique call since that is the call for the gateway
    for call in apply_calls[1:]:
        apply_args.append(call[0][0])
    assert apply_args == expected

    assert isinstance(harness.charm.model.unit.status, ActiveStatus)


def test_removal(harness, subprocess, mocked_client, get_deleted_resource_types, mocker):
    check_output = subprocess.check_output

    mocked_yaml_object = mocker.Mock()
    mocked_yaml_object.__class__ = "ResourceObjectFromYaml"
    mocker.patch('charm.codecs.load_all_yaml', return_value=[mocked_yaml_object, mocked_yaml_object])
    harness.set_leader(True)
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )

    harness.begin_with_initial_hooks()

    # Reset the mock so that the calls list does not include calls from handle_default_gateway that was called
    # with the config changed event
    mocked_client.reset_mock()
    harness.charm.on.remove.emit()

    expected_args = [
                "./istioctl",
                "manifest",
                "generate",
                "-s",
                "profile=minimal",
                "-s",
                f"values.global.istioNamespace={None}",
            ]

    assert len(check_output.call_args_list) == 1
    assert check_output.call_args_list[0].args == (expected_args,)
    assert check_output.call_args_list[0].kwargs == {}

    delete_calls = mocked_client.return_value.delete.call_args_list
    # The 2 mock objects at the end are the "resources" that get returned from the mocked load_all_yaml call when
    # loading the resources from the manifest.
    assert get_deleted_resource_types(delete_calls) == ['VirtualService', 'DestinationRule', 'Gateway', 'EnvoyFilter',
                                                        'RbacConfig', 'ResourceObjectFromYaml',
                                                        'ResourceObjectFromYaml']
    # Now test the exceptions that should be ignored
    # ApiError
    api_error = ApiError(response=mocker.MagicMock())
    # # ApiError with not found message should be ignored
    api_error.status.message = "something not found"
    mocked_client.return_value.delete.side_effect = api_error
    # mock out the _delete_existing_resource_objects method since we dont want the ApiError to be thrown there
    mocker.patch('charm.Operator._delete_existing_resource_objects')
    # Ensure we DO NOT raise the exception
    harness.charm.on.remove.emit()

    # ApiError with unauthorized message should be ignored
    api_error.status.message = "(Unauthorized)"
    mocked_client.return_value.delete.side_effect = api_error
    # Ensure we DO NOT raise the exception
    harness.charm.on.remove.emit()

    # Other ApiErrors should throw an exception
    api_error.status.message = "mocked ApiError"
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        harness.charm.on.remove.emit()


def test_handle_default_gateways(harness, mocked_client, get_deleted_resource_types):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)

    # Reset the mock to clear any calls via config changed that happened due to the above harness setup.
    mocked_client.reset_mock()

    harness.charm.on.config_changed.emit()
    delete_calls = mocked_client.return_value.delete.call_args_list
    assert get_deleted_resource_types(delete_calls) == ['Gateway']
