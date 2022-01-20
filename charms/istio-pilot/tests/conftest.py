import pytest
from charm import Operator
from ops.testing import Harness


class Helpers:
    @staticmethod
    def get_unique_calls(call_args_list):
        uniques = []
        for call in call_args_list:
            if call in uniques:
                continue
            else:
                uniques.append(call)
        return uniques

    @staticmethod
    def get_deleted_resource_types(delete_calls):
        deleted_resource_types = []
        for call in delete_calls:
            resource_type = call[0][0]
            deleted_resource_types.append(resource_type)
        return deleted_resource_types

    @staticmethod
    def compare_deleted_resource_names(actual, expected):
        if len(actual) != len(expected):
            return False
        else:
            return all(elem in expected for elem in actual)

    @staticmethod
    def calls_contain_namespace(calls, namespace):
        for call in calls:
            # Ensure the namespace is included in the call
            if call.kwargs['namespace'] != namespace:
                return False
        return True


@pytest.fixture(scope="session")
def helpers():
    return Helpers()


# autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


@pytest.fixture(autouse=True)
def mocked_list(mocked_client, mocker):
    mocked_resource_obj = mocker.Mock()

    def side_effect(*args, **kwargs):
        # List needs to return a list of at least one object of the passed in resource type
        # so that delete gets called
        # Lightkube uses the objects class name (the resource kind) to delete objects
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
