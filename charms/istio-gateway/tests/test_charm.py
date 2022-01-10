import pytest
import yaml
from charm import Operator
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness
from lightkube.core.exceptions import LoadResourceError, ApiError


def begin_noop(harness):
    # Most of the tests use these lines to kick things off
    harness.begin_with_initial_hooks()
    container = harness.model.unit.get_container('noop')
    harness.charm.on['noop'].pebble_ready.emit(container)


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


@pytest.fixture
def harness():
    return Harness(Operator)


@pytest.fixture(params=["ingress", "egress"])
def kind(request):
    return request.param


@pytest.fixture()
def configured_harness(harness, kind):
    harness.set_leader(True)

    harness.update_config({'kind': kind})
    harness.add_oci_resource(
        "noop",
        {
            "registrypath": "",
            "username": "",
            "password": "",
        },
    )
    rel_id = harness.add_relation("istio-pilot", "app")

    harness.add_relation_unit(rel_id, "app/0")
    data = {"service-name": "service-name", "service-port": '6666'}
    harness.update_relation_data(
        rel_id,
        "app",
        {"_supported_versions": "- v1", "data": yaml.dump(data)},
    )

    begin_noop(harness)

    return harness


@pytest.fixture()
def mocked_load_all_yaml(mocker):
    load_all_yaml = mocker.patch('charm.codecs.load_all_yaml', side_effect=LoadResourceError('mocked error'))
    yield load_all_yaml


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_no_kind(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == BlockedStatus('Config item `kind` must be set')


def test_kind_no_rel(harness):
    harness.set_leader(True)

    harness.update_config({'kind': 'ingress'})

    begin_noop(harness)

    assert harness.charm.model.unit.status == BlockedStatus('Waiting for istio-pilot relation')


def get_unique_calls(call_args_list):
    uniques = []
    for call in call_args_list:
        if call in uniques:
            continue
        else:
            uniques.append(call)

    return uniques


def test_install_apply(configured_harness, kind, mocked_client):
    actual_objects = []
    expected_objects = list(yaml.safe_load_all(open(f'tests/{kind}-example.yaml')))

    # The install method is invoked multiple times, and the apply method is called for every object in the manifest
    # but we will ignore the duplicated entries in the call list
    for call in get_unique_calls(mocked_client.return_value.apply.call_args_list):
        # Ensure the server side apply calls include the namespace kwarg
        assert call.kwargs['namespace'] == 'None'
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    assert expected_objects == actual_objects
    assert configured_harness.charm.model.unit.status == ActiveStatus('')


def test_install_apply_with_load_resource_error(configured_harness, kind, mocker):
    mocker.patch('charm.codecs.load_all_yaml', side_effect=LoadResourceError('mocked error'))
    # Ensure we raise the exception
    with pytest.raises(LoadResourceError):
        configured_harness.charm.on.remove.emit()


def test_removal(configured_harness, kind, mocked_client, mocker):
    configured_harness.charm.on.remove.emit()

    # Ensure the objects that get deleted are the objects defined in the example yaml files
    actual_kind_name_list = []
    expected_objects = list(yaml.safe_load_all(open(f'tests/{kind}-example.yaml')))
    expected_kind_name_list = []
    for obj in expected_objects:
        kind_name = {'kind': obj['kind'], 'name': obj['metadata']['name']}
        expected_kind_name_list.append(kind_name)

    for call in mocked_client.return_value.delete.call_args_list:
        # Ensure the delete calls include the namespace kwarg ('None' in the example yaml)
        assert call.kwargs['namespace'] == 'None'
        # The first argument is the resource class
        # The second argument is the object name
        kind_name = {'kind': call.args[0].__name__, 'name': call.args[1]}
        actual_kind_name_list.append(kind_name)

    assert expected_kind_name_list == actual_kind_name_list


def test_removal_with_load_resource_error(configured_harness, mocker):
    mocker.patch('charm.codecs.load_all_yaml', side_effect=LoadResourceError('mocked error'))
    # Ensure we raise the exception
    with pytest.raises(LoadResourceError):
        configured_harness.charm.on.remove.emit()


def test_removal_with_unauthorized_error(configured_harness, mocked_client, mocker):
    api_error = ApiError(response=mocker.MagicMock())
    api_error.status.message = "(Unauthorized)"
    mocked_client.return_value.delete.side_effect = api_error
    # Ensure we DO NOT raise the exception
    configured_harness.charm.on.remove.emit()
