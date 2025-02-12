import asyncio
import json
import os
import pwd
import shutil
from weakref import WeakValueDictionary

from traitlets import Float, Unicode, default

from ..base import ClusterManager


__all__ = ("JobQueueClusterManager",)


class JobQueueClusterManager(ClusterManager):
    """A base cluster manager for deploying Dask on a jobqueue cluster."""

    worker_setup = Unicode(
        "", help="Script to run before dask worker starts.", config=True
    )

    scheduler_setup = Unicode(
        "", help="Script to run before dask scheduler starts.", config=True
    )

    staging_directory = Unicode(
        "{home}/.dask-gateway/",
        help="""
        The staging directory for storing files before the job starts.

        A subdirectory will be created for each new cluster which will store
        temporary files for that cluster. On cluster shutdown the subdirectory
        will be removed.

        This field can be a template, which recieves the following fields:

        - home (the user's home directory)
        - username (the user's name)
        """,
        config=True,
    )

    status_poll_interval = Float(
        0.5,
        help="The interval (in seconds) in which to poll for job statuses.",
        config=True,
    )

    # The following fields are configurable only for just-in-case reasons. The
    # defaults should be sufficient for most users.

    dask_gateway_jobqueue_launcher = Unicode(
        help="The path to the dask-gateway-jobqueue-launcher executable", config=True
    )

    @default("dask_gateway_jobqueue_launcher")
    def _default_launcher_path(self):
        return (
            shutil.which("dask-gateway-jobqueue-launcher")
            or "dask-gateway-jobqueue-launcher"
        )

    submit_command = Unicode(help="The path to the job submit command", config=True)

    cancel_command = Unicode(help="The path to the job cancel command", config=True)

    status_command = Unicode(help="The path to the job status command", config=True)

    def get_worker_args(self):
        return [
            "--nthreads",
            str(self.worker_cores),
            "--memory-limit",
            str(self.worker_memory),
        ]

    @property
    def worker_command(self):
        """The full command (with args) to launch a dask worker"""
        return " ".join([self.worker_cmd] + self.get_worker_args())

    @property
    def scheduler_command(self):
        """The full command (with args) to launch a dask scheduler"""
        return self.scheduler_cmd

    def get_submit_cmd_env_stdin(self, cluster_info, worker_name=None):
        raise NotImplementedError

    def get_stop_cmd_env(self, job_id):
        raise NotImplementedError

    def get_status_cmd_env(self, job_ids):
        raise NotImplementedError

    def parse_job_id(self, stdout):
        raise NotImplementedError

    def parse_job_states(self, stdout):
        raise NotImplementedError

    def get_staging_directory(self, cluster_info):
        staging_dir = self.staging_directory.format(
            home=pwd.getpwnam(cluster_info.username).pw_dir,
            username=cluster_info.username,
        )
        return os.path.join(staging_dir, cluster_info.cluster_name)

    def get_tls_paths(self, cluster_info):
        """Get the absolute paths to the tls cert and key files."""
        staging_dir = self.get_staging_directory(cluster_info)
        cert_path = os.path.join(staging_dir, "dask.crt")
        key_path = os.path.join(staging_dir, "dask.pem")
        return cert_path, key_path

    async def do_as_user(self, user, action, **kwargs):
        cmd = ["sudo", "-nHu", user, self.dask_gateway_jobqueue_launcher]
        kwargs["action"] = action
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env={},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(json.dumps(kwargs).encode("utf8"))
        stdout = stdout.decode("utf8", "replace")
        stderr = stderr.decode("utf8", "replace")

        if proc.returncode != 0:
            raise Exception(
                "Error running `dask-gateway-jobqueue-launcher`\n"
                "  returncode: %d\n"
                "  stdout: %s\n"
                "  stderr: %s" % (proc.returncode, stdout, stderr)
            )
        result = json.loads(stdout)
        if not result["ok"]:
            raise Exception(result["error"])
        return result["returncode"], result["stdout"], result["stderr"]

    async def start_job(self, cluster_info, worker_name=None):
        cmd, env, stdin = self.get_submit_cmd_env_stdin(
            cluster_info, worker_name=worker_name
        )
        if not worker_name:
            staging_dir = self.get_staging_directory(cluster_info)
            files = {
                "dask.pem": cluster_info.tls_key.decode("utf8"),
                "dask.crt": cluster_info.tls_cert.decode("utf8"),
            }
        else:
            staging_dir = files = None

        code, stdout, stderr = await self.do_as_user(
            user=cluster_info.username,
            action="start",
            cmd=cmd,
            env=env,
            stdin=stdin,
            staging_dir=staging_dir,
            files=files,
        )
        if code != 0:
            raise Exception(
                (
                    "Failed to submit job to batch system\n"
                    "  exit_code: %d\n"
                    "  stdout: %s\n"
                    "  stderr: %s"
                )
                % (code, stdout, stderr)
            )
        return self.parse_job_id(stdout)

    async def stop_job(self, cluster_info, job_id, worker_name=None):
        cmd, env = self.get_stop_cmd_env(job_id)

        if not worker_name:
            staging_dir = self.get_staging_directory(cluster_info)
        else:
            staging_dir = None

        code, stdout, stderr = await self.do_as_user(
            user=cluster_info.username,
            action="stop",
            cmd=cmd,
            env=env,
            staging_dir=staging_dir,
        )
        if code != 0 and "Job has finished" not in stderr:
            raise Exception(
                "Failed to stop job_id %s" % (job_id, cluster_info.cluster_name)
            )

    async def job_state_tracker(self):
        while True:
            if self.jobs_to_track:
                self.log.debug("Polling status of %d jobs", len(self.jobs_to_track))
                cmd, env = self.get_status_cmd_env(self.jobs_to_track)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                stdout = stdout.decode("utf8", "replace")
                if proc.returncode != 0:
                    stderr = stderr.decode("utf8", "replace")
                    self.log.warning(
                        "Job status check failed with returncode %d, stderr: %s",
                        proc.returncode,
                        stderr,
                    )

                running, failed = self.parse_job_states(stdout)

                for job_id in running:
                    fut = self.jobs_to_track.pop(job_id, None)
                    if fut:
                        fut.set_result(True)
                for job_id in failed:
                    fut = self.jobs_to_track.pop(job_id, None)
                    if fut:
                        fut.set_result(False)

            await asyncio.sleep(self.status_poll_interval)

    def is_job_running(self, job_id):
        if not hasattr(self, "job_tracker"):
            self.jobs_to_track = WeakValueDictionary()
            self.job_tracker = self.task_pool.create_background_task(
                self.job_state_tracker()
            )

        if job_id not in self.jobs_to_track:
            loop = asyncio.get_running_loop()
            fut = self.jobs_to_track[job_id] = loop.create_future()
        else:
            fut = self.jobs_to_track[job_id]
        return fut

    async def start_cluster(self, cluster_info):
        job_id = await self.start_job(cluster_info)
        yield {"job_id": job_id}
        if not await self.is_job_running(job_id):
            raise Exception(
                "Job %s for cluster %s failed, see logs for more information"
                % (job_id, cluster_info.cluster_name)
            )

    async def stop_cluster(self, cluster_info, cluster_state):
        job_id = cluster_state.get("job_id")
        if job_id is None:
            return
        await self.stop_job(cluster_info, job_id)

    async def start_worker(self, worker_name, cluster_info, cluster_state):
        job_id = await self.start_job(cluster_info, worker_name=worker_name)
        yield {"job_id": job_id}
        if not await self.is_job_running(job_id):
            raise Exception(
                "Job %s for worker %s failed, see logs for more information"
                % (job_id, worker_name)
            )

    async def stop_worker(self, worker_name, worker_state, cluster_info, cluster_state):
        job_id = worker_state.get("job_id")
        if job_id is None:
            return
        await self.stop_job(cluster_info, job_id, worker_name=worker_name)
