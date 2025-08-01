#!/usr/bin/python3
# Copyright 2024 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        https://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import logging
import os
import packaging.version
import re
import shutil
import stat
import subprocess
import time


LOCAL_RUN_NO_CONTAINER = "LOCAL"


class Result:
    def __init__(self, stdout, stderr, exited):
        self.stdout = stdout
        self.stderr = stderr
        self.exited = exited
        self.return_code = exited

    @property
    def failed(self):
        return self.exited != 0


class Connection:
    def __init__(self, host, user, port, connect_timeout, key_filename=None):
        self.host = host
        self.user = user
        self.port = port
        self.connect_timeout = connect_timeout
        self.key_filename = key_filename
        self.key = _prepare_key_arg(key_filename)

    def __enter__(self):
        return self

    def get_ssh_command(self):
        return f"ssh {self.key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout={self.connect_timeout} -p {self.port} {self.user}@{self.host}"

    def run(self, command, warn=False, hide=True, echo=False, popen=False):
        if not command.startswith("ssh") and not command.startswith("scp"):
            command = f"{self.get_ssh_command()} '{command}'"

        logging.debug(command)
        if echo:
            print(command)

        if popen:
            return subprocess.Popen(command)
        else:
            try:
                proc = subprocess.run(
                    command, check=not warn, capture_output=True, shell=True
                )
                returncode = proc.returncode

            except subprocess.CalledProcessError as e:
                returncode = e.returncode
                if returncode != 255:
                    raise

            if returncode == 255:
                raise ConnectionError(f"Could not connect using command '{command}'")

            stdout = proc.stdout.decode()
            stderr = proc.stderr.decode()

            if not hide:
                print(stdout)
                print(stderr)

            return Result(stdout, stderr, returncode)

    def put(self, file, key_filename=None, local_path=".", remote_path="."):
        if not key_filename:
            key_filename = self.key_filename
        return put(self, file, key_filename, local_path, remote_path)

    def sudo(self, command, warn=False):
        sudo_command = f"sudo {command}"
        return self.run(sudo_command, warn)

    def __exit__(self, arg1, arg2, arg3):
        pass


class LocalNoConnection:
    def __enter__(self):
        return self

    def run(self, command, warn=False, hide=True, echo=False, popen=False):
        logging.debug(command)
        if echo:
            print(command)

        if popen:
            return subprocess.Popen(command)
        else:
            proc = subprocess.run(
                command, check=not warn, capture_output=True, shell=True
            )
            returncode = proc.returncode

            stdout = proc.stdout.decode()
            stderr = proc.stderr.decode()

            if not hide:
                print(stdout)
                print(stderr)

            return Result(stdout, stderr, returncode)

    def put(self, file, key_filename=None, local_path=".", remote_path="."):
        shutil.copy(os.path.join(local_path, file), remote_path)

    def sudo(self, command, warn=False):
        sudo_command = f"sudo {command}"
        return self.run(sudo_command, warn)

    def __exit__(self, arg1, arg2, arg3):
        pass


def _prepare_key_arg(key_filename):
    if key_filename:
        # Git doesn't track rw permissions, but the keyfile needs to be 600 for
        # scp to accept it, so fix that here.
        os.chmod(key_filename, stat.S_IRUSR | stat.S_IWUSR)
        return "-i %s" % key_filename
    else:
        return ""


def put(conn, file, key_filename=None, local_path=".", remote_path="."):
    key_arg = _prepare_key_arg(key_filename)
    cmd = (
        "scp %s -C -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P %s %s %s@%s:%s"
        % (
            key_arg,
            conn.port,
            os.path.join(local_path, file),
            conn.user,
            conn.host,
            remote_path,
        )
    )
    logging.debug(cmd)
    conn.run(cmd)


def run(conn, command, key_filename=None, warn=False):
    key_arg = _prepare_key_arg(key_filename)
    cmd = (
        "ssh %s -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=60 -p %s %s@%s %s"
        % (key_arg, conn.port, conn.user, conn.host, command)
    )
    logging.debug(cmd)
    result = conn.run(cmd, warn=warn)
    return result


class PortForward:
    user = None
    host = None
    port = None
    key_filename = None
    local_port = None
    remote_port = None

    args = None
    proc = None

    def __init__(self, conn, key_filename, local_port, remote_port):
        self.user = conn.user
        self.host = conn.host
        self.port = conn.port
        self.key_filename = key_filename
        self.local_port = local_port
        self.remote_port = remote_port

    def __enter__(self):
        try:
            key_arg = _prepare_key_arg(self.key_filename).split()
            self.args = (
                ["ssh", "-4", "-N", "-f"]
                + key_arg
                + [
                    "-L",
                    "%d:localhost:%d" % (self.local_port, self.remote_port),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-p",
                    "%d" % self.port,
                    "%s@%s" % (self.user, self.host),
                ]
            )
            self.proc = subprocess.Popen(self.args)
            # '-f' flag causes SSH to background itself. We wait until it does so.
            self.proc.wait()
            if self.proc.returncode != 0:
                raise subprocess.CalledProcessError(self.proc.returncode, self.args)
        except subprocess.CalledProcessError:
            self.proc = None
            raise

    def __exit__(self, arg1, arg2, arg3):
        if self.proc:
            subprocess.check_call(["pkill", "-xf", re.escape(" ".join(self.args))])


def new_tester_ssh_connection(setup_test_container):
    if setup_test_container.image_name == LOCAL_RUN_NO_CONTAINER:
        return LocalNoConnection()

    with Connection(
        host="localhost",
        user=setup_test_container.user,
        port=setup_test_container.port,
        key_filename=setup_test_container.key_filename,
        connect_timeout=60,
    ) as conn:

        ready = _probe_ssh_connection(conn)

        assert ready, "SSH connection can not be established. Aborting"
        return conn


def wait_for_container_boot(docker_container_id):
    assert docker_container_id is not None
    ready = False
    timeout = time.time() + 60 * 15
    while not ready and time.time() < timeout:
        time.sleep(5)
        output = subprocess.check_output(
            "docker logs {} 2>&1".format(docker_container_id), shell=True
        )

        # Check on the last few chars only, so that we can detect reboots
        # For Raspberry Pi OS, the tty prompt comes earlier than the SSH server, so wait for the later
        if re.search(
            "(Poky.* tty|Started.*OpenBSD Secure Shell server)",
            output.decode("utf-8")[-1000:],
            flags=re.MULTILINE,
        ):
            ready = True

    return ready


def _probe_ssh_connection(conn):
    ready = False
    timeout = time.time() + 60
    while not ready and time.time() < timeout:
        try:
            result = conn.run("true", hide=True)
            if result.exited == 0:
                ready = True
        except ConnectionError as e:
            if not "Could not connect using command" in str(e):
                raise e
            time.sleep(5)

    return ready


def version_is_minimum(mender_deb_version, min_version):
    try:
        version_parsed = packaging.version.Version(mender_deb_version)
    except packaging.version.InvalidVersion:
        # Indicates that 'mender_deb_version' is likely a string (branch name).
        # Always consider them higher than the minimum version.
        return True

    return version_parsed >= packaging.version.Version(min_version)
