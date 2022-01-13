import pytest
import yaml
from charm import Operator
from ops.testing import Harness


@pytest.fixture
def harness():
    return Harness(Operator)


@pytest.fixture()
def begin_noop():
    def func(harness):
        # Most of the tests use these lines to kick things off
        harness.begin_with_initial_hooks()
        container = harness.model.unit.get_container('noop')
        harness.charm.on['noop'].pebble_ready.emit(container)

    return func


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


# Autouse to prevent calling out to the k8s API via lightkube
@pytest.fixture(autouse=True)
def mocked_client(mocker):
    client = mocker.patch("charm.Client")
    yield client


@pytest.fixture(params=["ingress", "egress"])
def kind(request):
    return request.param


@pytest.fixture()
def configured_harness(harness, begin_noop, kind):
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
