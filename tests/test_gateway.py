import asyncio
import os
import signal

import pytest
from cryptography.fernet import Fernet

from dask_gateway import Gateway
from dask_gateway_server.app import DaskGateway
from dask_gateway_server.managers import ClusterManager
from dask_gateway_server.managers.inprocess import InProcessClusterManager
from dask_gateway_server.utils import random_port

from .utils import LocalTestingClusterManager, temp_gateway


class SlowStartClusterManager(ClusterManager):
    state_1 = {"state_1": 1}
    state_2 = {"state_2": 2}
    state_3 = {"state_3": 3}
    pause_time = 0.2
    stop_cluster_state = None

    async def start_cluster(self, cluster_info):
        yield self.state_1
        await asyncio.sleep(self.pause_time)
        yield self.state_2
        await asyncio.sleep(self.pause_time)
        yield self.state_3

    async def stop_cluster(self, cluster_info, cluster_state):
        self.stop_cluster_state = cluster_state


class FailStartClusterManager(ClusterManager):
    fail_stage = 1
    stop_cluster_state = None

    async def start_cluster(self, cluster_info):
        for i in range(3):
            if i == self.fail_stage:
                raise ValueError("Oh No")
            yield {"i": i}

    async def stop_cluster(self, cluster_info, cluster_state):
        self.stop_cluster_state = cluster_state


class SlowWorkerStartClusterManager(InProcessClusterManager):
    pause_time = 0.2
    stop_worker_state = None

    async def start_worker(self, worker_name, cluster_info, cluster_state):
        for i in range(3):
            yield {"i": i}
            await asyncio.sleep(self.pause_time)

    async def stop_worker(self, worker_name, worker_state, cluster_info, cluster_state):
        self.stop_worker_state = worker_state


class FailWorkerStartClusterManager(InProcessClusterManager):
    fail_stage = 1
    stop_worker_state = None

    async def start_worker(self, worker_name, cluster_info, cluster_state):
        for i in range(3):
            if i == self.fail_stage:
                raise ValueError("Oh No")
            yield {"i": i}

    async def stop_worker(self, worker_name, worker_state, cluster_info, cluster_state):
        self.stop_worker_state = worker_state


@pytest.mark.asyncio
async def test_shutdown_on_startup_error(tmpdir):
    # A configuration that will cause a failure at runtime (not init time)
    gateway = DaskGateway(
        gateway_url="tls://127.0.0.1:%d" % random_port(),
        private_url="http://127.0.0.1:%d" % random_port(),
        public_url="http://127.0.0.1:%d" % random_port(),
        temp_dir=str(tmpdir.join("dask-gateway")),
        tls_cert=str(tmpdir.join("tls_cert.pem")),
        authenticator_class="dask_gateway_server.auth.DummyAuthenticator",
    )
    with pytest.raises(SystemExit) as exc:
        gateway.initialize([])
        await gateway.start_or_exit()
    assert exc.value.code == 1


def test_db_encrypt_keys_required(tmpdir):
    with pytest.raises(ValueError) as exc:
        gateway = DaskGateway(
            gateway_url="tls://127.0.0.1:%d" % random_port(),
            private_url="http://127.0.0.1:%d" % random_port(),
            public_url="http://127.0.0.1:%d" % random_port(),
            temp_dir=str(tmpdir.join("dask-gateway")),
            db_url="sqlite:///%s" % tmpdir.join("dask_gateway.sqlite"),
            authenticator_class="dask_gateway_server.auth.DummyAuthenticator",
        )
        gateway.initialize([])

    assert "DASK_GATEWAY_ENCRYPT_KEYS" in str(exc.value)


def test_db_encrypt_keys_invalid(tmpdir):
    with pytest.raises(ValueError) as exc:
        gateway = DaskGateway(
            gateway_url="tls://127.0.0.1:%d" % random_port(),
            private_url="http://127.0.0.1:%d" % random_port(),
            public_url="http://127.0.0.1:%d" % random_port(),
            temp_dir=str(tmpdir.join("dask-gateway")),
            db_url="sqlite:///%s" % tmpdir.join("dask_gateway.sqlite"),
            db_encrypt_keys=["abc"],
            authenticator_class="dask_gateway_server.auth.DummyAuthenticator",
        )
        gateway.initialize([])

    assert "DASK_GATEWAY_ENCRYPT_KEYS" in str(exc.value)


def test_db_decrypt_keys_from_env(monkeypatch):
    keys = [Fernet.generate_key(), Fernet.generate_key()]
    val = b";".join(keys).decode()
    monkeypatch.setenv("DASK_GATEWAY_ENCRYPT_KEYS", val)
    gateway = DaskGateway()
    assert gateway.db_encrypt_keys == keys


def test_resume_clusters_forbid_in_memory_db(tmpdir):
    with pytest.raises(ValueError) as exc:
        DaskGateway(
            gateway_url="tls://127.0.0.1:%d" % random_port(),
            private_url="http://127.0.0.1:%d" % random_port(),
            public_url="http://127.0.0.1:%d" % random_port(),
            temp_dir=str(tmpdir.join("dask-gateway")),
            db_url="sqlite://",
            stop_clusters_on_shutdown=False,
            authenticator_class="dask_gateway_server.auth.DummyAuthenticator",
        )

    assert "stop_clusters_on_shutdown" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_timeout,state", [(0.1, "state_1"), (0.25, "state_2")])
async def test_slow_cluster_start(tmpdir, start_timeout, state):

    async with temp_gateway(
        cluster_manager_class=SlowStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.cluster_start_timeout = start_timeout

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            # Submission fails due to start timeout
            cluster_id = await gateway.submit()
            with pytest.raises(Exception) as exc:
                await gateway.connect(cluster_id)
            assert cluster_id in str(exc.value)

            # Stop cluster called with last reported state
            res = getattr(gateway_proc.cluster_manager, state)
            assert gateway_proc.cluster_manager.stop_cluster_state == res


@pytest.mark.asyncio
async def test_slow_cluster_connect(tmpdir):

    async with temp_gateway(
        cluster_manager_class=SlowStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.cluster_connect_timeout = 0.1
        gateway_proc.cluster_manager.pause_time = 0

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            # Submission fails due to connect timeout
            cluster_id = await gateway.submit()
            with pytest.raises(Exception) as exc:
                await gateway.connect(cluster_id)
            assert cluster_id in str(exc.value)

            # Stop cluster called with last reported state
            res = gateway_proc.cluster_manager.state_3
            assert gateway_proc.cluster_manager.stop_cluster_state == res


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", [0, 1])
async def test_failing_cluster_start(tmpdir, fail_stage):

    async with temp_gateway(
        cluster_manager_class=FailStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.fail_stage = fail_stage

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            # Submission fails due to error during start
            cluster_id = await gateway.submit()
            with pytest.raises(Exception) as exc:
                await gateway.connect(cluster_id)
            assert cluster_id in str(exc.value)

            # Stop cluster called with last reported state
            res = {} if fail_stage == 0 else {"i": fail_stage - 1}
            assert gateway_proc.cluster_manager.stop_cluster_state == res


@pytest.mark.asyncio
@pytest.mark.parametrize("start_timeout,state", [(0.1, 0), (0.25, 1)])
async def test_slow_worker_start(tmpdir, start_timeout, state):

    async with temp_gateway(
        cluster_manager_class=SlowWorkerStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.worker_start_timeout = start_timeout

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:
            cluster = await gateway.new_cluster()
            await cluster.scale(1)

            # Wait for worker failure
            timeout = 5
            while timeout > 0:
                if gateway_proc.cluster_manager.stop_worker_state is not None:
                    break
                await asyncio.sleep(0.1)
                timeout -= 0.1
            else:
                assert False, "Operation timed out"

            # Stop worker called with last reported state
            assert gateway_proc.cluster_manager.stop_worker_state == {"i": state}

            # Stop the cluster
            await cluster.shutdown()


@pytest.mark.asyncio
async def test_slow_worker_connect(tmpdir):

    async with temp_gateway(
        cluster_manager_class=SlowWorkerStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.worker_connect_timeout = 0.1
        gateway_proc.cluster_manager.pause_time = 0

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:
            cluster = await gateway.new_cluster()
            await cluster.scale(1)

            # Wait for worker failure
            timeout = 5
            while timeout > 0:
                if gateway_proc.cluster_manager.stop_worker_state is not None:
                    break
                await asyncio.sleep(0.1)
                timeout -= 0.1
            else:
                assert False, "Operation timed out"

            # Stop worker called with last reported state
            assert gateway_proc.cluster_manager.stop_worker_state == {"i": 2}

            # Stop the cluster
            await cluster.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", [0, 1])
async def test_failing_worker_start(tmpdir, fail_stage):

    async with temp_gateway(
        cluster_manager_class=FailWorkerStartClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        gateway_proc.cluster_manager.fail_stage = fail_stage

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:
            cluster = await gateway.new_cluster()
            await cluster.scale(1)

            # Wait for worker failure
            timeout = 5
            while timeout > 0:
                if gateway_proc.cluster_manager.stop_worker_state is not None:
                    break
                await asyncio.sleep(0.1)
                timeout -= 0.1
            else:
                assert False, "Operation timed out"

            # Stop worker called with last reported state
            res = {} if fail_stage == 0 else {"i": fail_stage - 1}
            assert gateway_proc.cluster_manager.stop_worker_state == res

            # Stop the cluster
            await cluster.shutdown()


@pytest.mark.asyncio
async def test_successful_cluster(tmpdir):
    async with temp_gateway(
        cluster_manager_class=InProcessClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            cluster = await gateway.new_cluster()
            await cluster.scale(2)

            with cluster.get_client(set_as_default=False) as client:
                res = await client.submit(lambda x: x + 1, 1)
                assert res == 2

            await cluster.scale(1)

            with cluster.get_client(set_as_default=False) as client:
                res = await client.submit(lambda x: x + 1, 1)
                assert res == 2

            await cluster.shutdown()


@pytest.mark.asyncio
async def test_gateway_stop_clusters_on_shutdown(tmpdir):
    async with temp_gateway(
        cluster_manager_class=InProcessClusterManager,
        temp_dir=str(tmpdir.join("dask-gateway")),
    ) as gateway_proc:

        manager = gateway_proc.cluster_manager

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            await gateway.new_cluster()
            cluster2 = await gateway.new_cluster()
            await cluster2.shutdown()

            # There are active clusters
            assert manager.active_schedulers

    # Active clusters are stopped on shutdown
    assert not manager.active_schedulers


@pytest.mark.asyncio
async def test_gateway_resume_clusters_after_shutdown(tmpdir):
    temp_dir = str(tmpdir.join("dask-gateway"))
    os.mkdir(temp_dir, mode=0o700)

    db_url = "sqlite:///%s" % tmpdir.join("dask_gateway.sqlite")
    db_encrypt_keys = [Fernet.generate_key()]

    async with temp_gateway(
        cluster_manager_class=LocalTestingClusterManager,
        temp_dir=temp_dir,
        db_url=db_url,
        db_encrypt_keys=db_encrypt_keys,
        stop_clusters_on_shutdown=False,
    ) as gateway_proc:

        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            cluster1_name = await gateway.submit()
            cluster1 = await gateway.connect(cluster1_name)
            await cluster1.scale(2)

            cluster2_name = await gateway.submit()
            await gateway.connect(cluster2_name)

            cluster3 = await gateway.new_cluster()
            await cluster3.shutdown()

    active_clusters = {c.name: c for c in gateway_proc.db.active_clusters()}

    # Active clusters are not stopped on shutdown
    assert active_clusters

    # Stop 1 worker in cluster 1
    worker = list(active_clusters[cluster1_name].workers.values())[0]
    pid = worker.state["pid"]
    os.kill(pid, signal.SIGTERM)

    # Stop cluster 2
    pid = active_clusters[cluster2_name].state["pid"]
    os.kill(pid, signal.SIGTERM)

    # Restart a new temp_gateway
    async with temp_gateway(
        cluster_manager_class=LocalTestingClusterManager,
        temp_dir=temp_dir,
        db_url=db_url,
        db_encrypt_keys=db_encrypt_keys,
        stop_clusters_on_shutdown=False,
        gateway_url=gateway_proc.gateway_url,
        private_url=gateway_proc.private_url,
        public_url=gateway_proc.public_url,
        check_cluster_timeout=2,
    ) as gateway_proc:

        active_clusters = list(gateway_proc.db.active_clusters())
        assert len(active_clusters) == 1

        cluster = active_clusters[0]

        assert cluster.name == cluster1_name
        assert len(cluster.active_workers) == 1

        # Check that cluster is available and everything still works
        async with Gateway(
            address=gateway_proc.public_url, asynchronous=True
        ) as gateway:

            cluster = await gateway.connect(cluster1_name)

            with cluster.get_client(set_as_default=False) as client:
                res = await client.submit(lambda x: x + 1, 1)
                assert res == 2

            await cluster.shutdown()
