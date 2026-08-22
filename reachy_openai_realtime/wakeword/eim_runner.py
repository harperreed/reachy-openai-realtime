# ABOUTME: Vendored client for an Edge Impulse `.eim` runner binary — spawns it,
# ABOUTME: speaks NUL-terminated JSON over a Unix socket. No edge-impulse-linux dep (addendum A1).
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelParameters:
    frequency: int
    input_features_count: int
    slice_size: int
    labels: list[str]


class EimRunner:
    """Runs a `.eim` file (which is itself the runner executable) and talks to
    it over a Unix socket. `sendall` is mandatory — a bare send truncates the
    ~100 KB classify payload. Responses are NUL-terminated JSON."""

    def __init__(self, model_path: str) -> None:
        self._model_path = model_path
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._tmpdir: str | None = None
        self._id = 0
        self.parameters: ModelParameters | None = None

    def start(self, *, connect_timeout: float = 10.0) -> ModelParameters:
        mode = os.stat(self._model_path).st_mode
        os.chmod(self._model_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._tmpdir = tempfile.mkdtemp(prefix="reachy-eim-")
        sock_path = os.path.join(self._tmpdir, "runner.sock")
        self._proc = subprocess.Popen(
            [self._model_path, sock_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + connect_timeout
        while not os.path.exists(sock_path):
            if self._proc.poll() is not None:
                raise RuntimeError(f"eim runner exited early: {self._proc.returncode}")
            if time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for eim runner socket")
            time.sleep(0.05)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout)
        self._sock.connect(sock_path)
        reply = self._roundtrip({"hello": 1})
        params = reply["model_parameters"]
        self.parameters = ModelParameters(
            frequency=int(params["frequency"]),
            input_features_count=int(params["input_features_count"]),
            slice_size=int(params["slice_size"]),
            labels=list(params["labels"]),
        )
        return self.parameters

    def classify(self, features: list[int]) -> dict[str, float]:
        reply = self._roundtrip({"classify": features})
        return reply["result"]["classification"]

    def _roundtrip(self, message: dict) -> dict:
        if self._sock is None:
            raise RuntimeError("EimRunner not started")
        self._id += 1
        payload = json.dumps({**message, "id": self._id}).encode()
        self._sock.sendall(payload)  # bare send() truncates ~100 KB payloads
        data = b""
        while not data.endswith(b"\x00"):
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("eim runner closed the socket")
            data += chunk
        return json.loads(data[:-1])

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.send_signal(subprocess.signal.SIGINT)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
