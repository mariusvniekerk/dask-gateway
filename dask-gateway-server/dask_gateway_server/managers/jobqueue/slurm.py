import math
import os
import shutil

from traitlets import Unicode, default

from .base import JobQueueClusterManager


__all__ = ("SlurmClusterManager",)


def slurm_format_memory(n):
    """Format memory in bytes for use with slurm."""
    if n >= 10 * (1024 ** 3):
        return "%dG" % math.ceil(n / (1024 ** 3))
    if n >= 10 * (1024 ** 2):
        return "%dM" % math.ceil(n / (1024 ** 2))
    if n >= 10 * 1024:
        return "%dK" % math.ceil(n / 1024)
    return "1K"


class SlurmClusterManager(JobQueueClusterManager):
    """A cluster manager for deploying Dask on a Slurm cluster."""

    partition = Unicode("", help="The partition to submit jobs to.", config=True)

    qos = Unicode("", help="QOS string associated with each job.", config=True)

    account = Unicode("", help="Account string associated with each job.", config=True)

    @default("submit_command")
    def _default_submit_command(self):
        return shutil.which("sbatch") or "sbatch"

    @default("cancel_command")
    def _default_cancel_command(self):
        return shutil.which("scancel") or "scancel"

    @default("status_command")
    def _default_status_command(self):
        return shutil.which("squeue") or "squeue"

    def get_submit_cmd_env_stdin(self, cluster_info, worker_name=None):
        env = self.get_env(cluster_info)

        cmd = [self.submit_command, "--parsable"]
        cmd.append("--job-name=dask-gateway")
        if self.partition:
            cmd.append("--partition=" + self.partition)
        if self.account:
            cmd.account("--account=" + self.account)
        if self.qos:
            cmd.extend("--qos=" + self.qos)

        if worker_name:
            env["DASK_GATEWAY_WORKER_NAME"] = worker_name
            cpus = self.worker_cores
            mem = slurm_format_memory(self.worker_memory)
            log_file = "dask-worker-%s.log" % worker_name
            script = "\n".join(["#!/bin/sh", self.worker_setup, self.worker_command])
        else:
            cpus = self.scheduler_cores
            mem = slurm_format_memory(self.scheduler_memory)
            log_file = "dask-scheduler-%s.log" % cluster_info.cluster_name
            script = "\n".join(
                ["#!/bin/sh", self.scheduler_setup, self.scheduler_command]
            )

        staging_dir = self.get_staging_directory(cluster_info)

        cmd.extend(
            [
                "--chdir=" + staging_dir,
                "--output=" + os.path.join(staging_dir, log_file),
                "--cpus-per-task=%d" % cpus,
                "--mem=%s" % mem,
                "--export=%s" % (",".join(sorted(env))),
            ]
        )

        return cmd, env, script

    def get_stop_cmd_env(self, job_id):
        return [self.cancel_command, job_id], {}

    def get_status_cmd_env(self, job_ids):
        cmd = [self.status_command, "-h", "--job=%s" % ",".join(job_ids), "-o", "%i %t"]
        return cmd, {}

    def parse_job_id(self, stdout):
        return stdout.strip()

    def parse_job_states(self, stdout):
        running = []
        failed = []

        for l in stdout.splitlines():
            job_id, state = l.split()
            if state in ("R", "CG"):
                running.append(job_id)
            elif state not in ("PD", "CF"):
                failed.append(job_id)
        return running, failed
