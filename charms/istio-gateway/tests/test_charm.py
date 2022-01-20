import pytest
import yaml
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from lightkube.core.exceptions import ApiError


def test_not_leader(harness):
    harness.begin()
    assert harness.charm.model.unit.status == WaitingStatus('Waiting for leadership')


def test_no_kind(harness):
    harness.set_leader(True)
    harness.begin_with_initial_hooks()
    assert harness.charm.model.unit.status == BlockedStatus('Config item `kind` must be set')


def test_kind_no_rel(harness, helpers):
    harness.set_leader(True)
    harness.update_config({'kind': 'ingress'})
    helpers.begin_noop(harness)

    assert harness.charm.model.unit.status == BlockedStatus('Waiting for istio-pilot relation')


def test_install_apply(configured_harness, kind, mocked_client, helpers):
    actual_objects = []
    expected_objects = list(yaml.safe_load_all(open(f'tests/{kind}-example.yaml')))

    # The install method is invoked multiple times, and the apply method is called for every object
    # in the manifest
    # but we will ignore the duplicated entries in the call list
    for call in helpers.get_unique_calls(mocked_client.return_value.apply.call_args_list):
        # Ensure the server side apply calls include the namespace kwarg
        assert call.kwargs['namespace'] == 'None'
        # The first (and only) argument to the apply method is the obj
        # Convert the object to a dictionary and add it to the list
        actual_objects.append(call.args[0].to_dict())

    assert expected_objects == actual_objects
    assert configured_harness.charm.model.unit.status == ActiveStatus('')


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

    # Test exceptions
    # ApiError with unauthorized message should be ignored
    api_error = ApiError(response=mocker.MagicMock())
    api_error.status.message = "(Unauthorized)"
    mocked_client.return_value.delete.side_effect = api_error
    # Ensure we DO NOT raise the exception
    configured_harness.charm.on.remove.emit()

    # Other ApiErrors should raise exceptions
    api_error = ApiError(response=mocker.MagicMock())
    api_error.status.message = "mocked error"
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        configured_harness.charm.on.remove.emit()

    # Test with nonexistent status message
    api_error.status.message = None
    mocked_client.return_value.delete.side_effect = api_error
    with pytest.raises(ApiError):
        configured_harness.charm.on.remove.emit()
