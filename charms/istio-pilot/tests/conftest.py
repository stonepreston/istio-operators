import pytest
from charm import Operator
from ops.testing import Harness


@pytest.fixture()
def get_unique_calls():
    def func(call_args_list):
        uniques = []
        for call in call_args_list:
            if call in uniques:
                continue
            else:
                uniques.append(call)
        return uniques

    return func


@pytest.fixture()
def get_deleted_resource_types():
    def func(delete_calls):
        deleted_resource_types = []
        for call in delete_calls:
            resource_type = call[0][0]
            deleted_resource_types.append(resource_type)
        return deleted_resource_types

    return func


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


@pytest.fixture(autouse=True)
def mocked_list(mocked_client, mocker):
    # When looking up services, list needs to return a subscriptable (MagicMock mocks are subscriptable) object
    # that has an IP attribute equal to 127.0.0.1
    mocked_ingress = mocker.MagicMock()
    mocked_ingress.ip = "127.0.0.1"
    mocked_service_obj = mocker.MagicMock()
    mocked_service_obj.status.loadBalancer.ingress.__getitem__.return_value = mocked_ingress

    # Otherwise, list needs to return a list of at least one object
    mocked_resource_obj = mocker.Mock()

    def side_effect(*args, **kwargs):
        if args[0].__name__ == "Service":
            return [mocked_service_obj]
        else:
            # List needs to return a list of at least one object of the passed in resource type
            # so that delete gets called
            # Lightkube uses the objects class name (which should be the resource kind) to delete objects
            # We need the list objects' class names to match the resource that was passed in to
            # the list method
            mocked_resource_obj.__class__ = args[0].__name__
            return [mocked_resource_obj]

    mocked_client.return_value.list.side_effect = side_effect


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
