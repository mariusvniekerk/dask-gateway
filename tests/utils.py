import asyncio
import atexit
import json
import os
import signal
import time
import uuid

import pytest

from tornado import web
from traitlets.config import Config

from dask_gateway_server.app import DaskGateway
from dask_gateway_server.utils import random_port, get_ip
from dask_gateway_server.tls import new_keypair
from dask_gateway_server.objects import (
    Cluster,
    User,
    Worker,
    ClusterStatus,
    WorkerStatus,
)
from dask_gateway_server.managers.local import UnsafeLocalClusterManager


_PIDS = set()


@atexit.register
def cleanup_lost_processes():
    if not _PIDS:
        return
    nkilled = 0
    for pid in _PIDS:
        try:
            os.kill(pid, signal.SIGTERM)
            nkilled += 1
        except OSError:
            pass
    if nkilled:
        print("-- Stopped %d lost processes --" % nkilled)


class LocalTestingClusterManager(UnsafeLocalClusterManager):
    async def start_process(self, *args, **kwargs):
        pid = await super().start_process(*args, **kwargs)
        _PIDS.add(pid)
        return pid

    async def stop_process(self, pid):
        await super().stop_process(pid)
        _PIDS.discard(pid)


class MockGateway(object):
    def __init__(self):
        self.clusters = {}

    def new_cluster(self):
        cluster_name = uuid.uuid4().hex
        cert, key = new_keypair(cluster_name)
        cluster = Cluster(
            name=cluster_name,
            user=User(name="alice"),
            token=uuid.uuid4().hex,
            tls_cert=cert,
            tls_key=key,
            state={},
            status=ClusterStatus.STARTING,
        )
        self.clusters[cluster_name] = cluster
        return cluster

    def get_cluster(self, cluster_name):
        return self.clusters[cluster_name]

    def mark_cluster_started(self, cluster_name, json_data):
        cluster = self.clusters[cluster_name]
        for k in ["scheduler_address", "dashboard_address", "api_address"]:
            setattr(cluster, k, json_data[k])
        cluster._connect_future.set_result(True)

    def mark_cluster_stopped(self, cluster_name):
        del self.clusters[cluster_name]

    def new_worker(self, cluster_name):
        cluster = self.get_cluster(cluster_name)
        worker = Worker(
            name=uuid.uuid4().hex,
            cluster=cluster,
            state={},
            status=WorkerStatus.STARTING,
        )
        cluster.workers[worker.name] = worker
        return worker

    def mark_worker_started(self, cluster_name, worker_name):
        cluster = self.get_cluster(cluster_name)
        worker = cluster.workers[worker_name]
        worker._connect_future.set_result(True)

    def mark_worker_stopped(self, cluster_name, worker_name):
        cluster = self.get_cluster(cluster_name)
        cluster.workers.pop(worker_name)


class BaseHandler(web.RequestHandler):
    @property
    def gateway(self):
        return self.settings["gateway"]

    def prepare(self):
        if self.request.headers.get("Content-Type", "").startswith("application/json"):
            self.json_data = json.loads(self.request.body)
        else:
            self.json_data = None

    def assert_token_matches(self, cluster_name):
        cluster = self.gateway.get_cluster(cluster_name)
        auth_header = self.request.headers.get("Authorization")
        assert auth_header
        auth_type, auth_key = auth_header.split(" ", 1)
        assert auth_type == "token"
        assert auth_key == cluster.info.api_token


class ClusterRegistrationHandler(BaseHandler):
    async def put(self, cluster_name):
        self.assert_token_matches(cluster_name)
        self.gateway.mark_cluster_started(cluster_name, self.json_data)

    async def get(self, cluster_name):
        self.assert_token_matches(cluster_name)
        cluster = self.gateway.get_cluster(cluster_name)
        self.write(
            {
                "scheduler_address": cluster.scheduler_address,
                "dashboard_address": cluster.dashboard_address,
                "api_address": cluster.api_address,
            }
        )


class ClusterWorkersHandler(BaseHandler):
    async def put(self, cluster_name, worker_name):
        self.assert_token_matches(cluster_name)
        self.gateway.mark_worker_started(cluster_name, worker_name)

    async def delete(self, cluster_name, worker_name):
        self.assert_token_matches(cluster_name)
        self.gateway.mark_worker_stopped(cluster_name, worker_name)


def gateway_test(func):
    async def inner(self, tmpdir):
        port = random_port()
        host = get_ip()
        api_url = "http://%s:%d" % (host, port)
        gateway = MockGateway()
        app = web.Application(
            [
                (
                    "/clusters/([a-zA-Z0-9-_.]*)/workers/([a-zA-Z0-9-_.]*)",
                    ClusterWorkersHandler,
                ),
                ("/clusters/([a-zA-Z0-9-_.]*)/addresses", ClusterRegistrationHandler),
            ],
            gateway=gateway,
        )
        manager = self.new_manager(api_url=api_url, temp_dir=str(tmpdir))
        server = None
        try:
            server = app.listen(port, address=host)
            await func(self, gateway, manager)
        finally:
            if server is not None:
                server.stop()
            for cluster in gateway.clusters.values():
                worker_states = [w.state for w in cluster.workers.values()]
                await self.cleanup_cluster(
                    manager, cluster.info, cluster.state, worker_states
                )
            await manager.task_pool.close()

        # Only raise if test didn't fail earlier
        if gateway.clusters:
            assert False, "Clusters %r not fully cleaned up" % list(gateway.clusters)

    inner.__name__ = func.__name__

    return inner


class ClusterManagerTests(object):
    async def cleanup_cluster(
        self, manager, cluster_info, cluster_state, worker_states
    ):
        raise NotImplementedError

    def new_manager(self, **kwargs):
        raise NotImplementedError

    def cluster_is_running(self, manager, cluster_info, cluster_state):
        raise NotImplementedError

    def worker_is_running(self, manager, cluster_info, cluster_state, worker_state):
        raise NotImplementedError

    def num_start_cluster_stages(self):
        raise NotImplementedError

    def num_start_worker_stages(self):
        raise NotImplementedError

    def cluster_is_stopped(self, manager, cluster_info, cluster_state):
        # Wait for 30 seconds, pinging every 1/4 second
        for i in range(120):
            if not self.cluster_is_running(manager, cluster_info, cluster_state):
                return True
            time.sleep(0.25)
        return False

    def worker_is_stopped(self, manager, cluster_info, cluster_state, worker_state):
        # Wait for 30 seconds, pinging every 1/4 second
        for i in range(120):
            if not self.worker_is_running(
                manager, cluster_info, cluster_state, worker_state
            ):
                return True
            time.sleep(0.25)
        return False

    @pytest.mark.asyncio
    @gateway_test
    async def test_start_stop_cluster(self, gateway, manager):
        # Create a new cluster
        cluster = gateway.new_cluster()

        # Start the cluster
        async for state in manager.start_cluster(cluster.info):
            cluster.state = state

        # Wait for connection
        await asyncio.wait_for(cluster._connect_future, manager.cluster_connect_timeout)
        assert self.cluster_is_running(manager, cluster.info, cluster.state)

        # Stop the cluster
        await manager.stop_cluster(cluster.info, cluster.state)
        assert self.cluster_is_stopped(manager, cluster.info, cluster.state)
        gateway.mark_cluster_stopped(cluster.name)

    async def check_cancel_during_cluster_startup(self, gateway, manager, fail_stage):
        # Create a new cluster
        cluster = gateway.new_cluster()

        # Start the cluster
        start_task = manager.start_cluster(cluster.info)
        if fail_stage > 0:
            i = 1
            async for state in start_task:
                cluster.state = state
                if i == fail_stage:
                    break
                i += 1

        # Cleanup cancelled async generator
        await start_task.athrow(GeneratorExit)

        # Stop the cluster
        await manager.stop_cluster(cluster.info, cluster.state)
        assert self.cluster_is_stopped(manager, cluster.info, cluster.state)
        gateway.mark_cluster_stopped(cluster.name)

    @pytest.mark.asyncio
    @gateway_test
    async def test_cancel_during_cluster_startup(self, gateway, manager):
        for fail_stage in range(self.num_start_cluster_stages()):
            await self.check_cancel_during_cluster_startup(gateway, manager, fail_stage)

    @pytest.mark.asyncio
    @gateway_test
    async def test_start_stop_worker(self, gateway, manager):
        # Create a new cluster
        cluster = gateway.new_cluster()

        # Start the cluster
        async for state in manager.start_cluster(cluster.info):
            cluster.state = state

        # Wait for connection
        await asyncio.wait_for(cluster._connect_future, manager.cluster_connect_timeout)
        assert self.cluster_is_running(manager, cluster.info, cluster.state)

        # Create a new worker
        worker = gateway.new_worker(cluster.name)

        # Start the worker
        async for state in manager.start_worker(
            worker.name, cluster.info, cluster.state
        ):
            worker.state = state

        # Wait for worker to connect
        await asyncio.wait_for(worker._connect_future, manager.worker_connect_timeout)
        assert self.worker_is_running(
            manager, cluster.info, cluster.state, worker.state
        )

        # Stop the worker
        await manager.stop_worker(
            worker.name, worker.state, cluster.info, cluster.state
        )
        assert self.worker_is_stopped(
            manager, cluster.info, cluster.state, worker.state
        )
        gateway.mark_worker_stopped(cluster.name, worker.name)

        # Stop the cluster
        await manager.stop_cluster(cluster.info, cluster.state)
        assert self.cluster_is_stopped(manager, cluster.info, cluster.state)
        gateway.mark_cluster_stopped(cluster.name)

    async def check_cancel_during_worker_startup(self, gateway, manager, fail_stage):
        # Create a new cluster
        cluster = gateway.new_cluster()

        # Start the cluster
        async for state in manager.start_cluster(cluster.info):
            cluster.state = state

        # Wait for connection
        await asyncio.wait_for(cluster._connect_future, manager.cluster_connect_timeout)
        assert self.cluster_is_running(manager, cluster.info, cluster.state)

        # Create a new worker
        worker = gateway.new_worker(cluster.name)

        # Start the worker
        start_task = manager.start_worker(worker.name, cluster.info, cluster.state)
        if fail_stage > 0:
            i = 1
            async for state in start_task:
                cluster.state = state
                if i == fail_stage:
                    break
                i += 1

        # Cleanup cancelled async generator
        await start_task.athrow(GeneratorExit)

        # Stop the worker
        await manager.stop_worker(
            worker.name, worker.state, cluster.info, cluster.state
        )
        assert self.worker_is_stopped(
            manager, cluster.info, cluster.state, worker.state
        )
        gateway.mark_worker_stopped(cluster.name, worker.name)

        # Stop the cluster
        await manager.stop_cluster(cluster.info, cluster.state)
        assert self.cluster_is_stopped(manager, cluster.info, cluster.state)
        gateway.mark_cluster_stopped(cluster.name)

    @pytest.mark.asyncio
    @gateway_test
    async def test_cancel_during_worker_startup(self, gateway, manager):
        for fail_stage in range(self.num_start_worker_stages()):
            await self.check_cancel_during_worker_startup(gateway, manager, fail_stage)


class temp_gateway(object):
    def __init__(self, **kwargs):
        config = Config()
        config2 = kwargs.pop("config", None)

        options = {
            "gateway_url": "tls://127.0.0.1:%d" % random_port(),
            "private_url": "http://127.0.0.1:%d" % random_port(),
            "public_url": "http://127.0.0.1:%d" % random_port(),
            "db_url": "sqlite:///:memory:",
            "authenticator_class": "dask_gateway_server.auth.DummyAuthenticator",
        }
        options.update(kwargs)
        config["DaskGateway"].update(options)

        if config2:
            config.merge(config2)

        self.config = config

    async def __aenter__(self):
        self.gateway = DaskGateway.instance(config=self.config)
        self.gateway.initialize([])
        await self.gateway.start_async()
        return self.gateway

    async def __aexit__(self, *args):
        await self.gateway.stop_async(stop_event_loop=False)
        DaskGateway.clear_instance()
