import csv
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generator, List, Literal, Optional

import pytest
import requests
from influxdb_client import InfluxDBClient
from yarl import URL

from .conftest import MainProcessFixture, OPCServer

INFLUXDB_HOST = "influxdb"
INFLUXDB_DB = "test_bucket"
INFLUXDB_TOKEN = (
    "zsQmRXoNWcQU4jsJxGOMQqwu5KLNGUhsxg4KZ2YRypNP"  # noqa: S105
    "C8FV7VUlygO4YndqHFlY4KwoOe5Dt0nrosEvDJYkiQ=="
)


@dataclass
class InfluxDBQuery:
    method: Literal["GET", "POST"]
    statement: str
    db: Optional[str] = None


class InfluxDB:
    def __init__(self) -> None:
        self.root_url = URL(f"http://{INFLUXDB_HOST}:8086")
        self.client = InfluxDBClient(self.root_url, token=INFLUXDB_TOKEN, org="testorg")

    def url(self, endpoint: str) -> str:
        return str(self.root_url / endpoint)

    def query(self, query: InfluxDBQuery) -> List[Dict[str, Any]]:
        url_params = {"q": query.statement}
        if query.db:
            url_params["db"] = query.db
        resp = requests.request(
            query.method,
            self.url("query"),
            headers={"Accept": "application/csv"},
            params=url_params,
        )
        resp.raise_for_status()
        return list(csv.DictReader(resp.text.splitlines()))

    def ping(self) -> bool:
        resp = requests.get(self.url("ready"))
        if resp.status_code != 200:
            return False
        resp = requests.get(self.url("api/v2/setup"))
        return resp.json()["allowed"] is False


@pytest.fixture()
def influxdb() -> Generator[InfluxDB, None, None]:
    _influxdb = InfluxDB()
    while not _influxdb.ping():
        time.sleep(0.1)
    yield _influxdb
    _influxdb.query(InfluxDBQuery("POST", f"CREATE DATABASE {INFLUXDB_DB}"))
    _influxdb.query(InfluxDBQuery("POST", f"DROP DATABASE {INFLUXDB_DB}"))


def test_smoketest(
    influxdb: InfluxDB,
    main_process: MainProcessFixture,
    mandatory_env_args: Dict[str, str],
    opcserver: OPCServer,
) -> None:
    envargs = dict(
        mandatory_env_args,
        INFLUX_DB_NAME=INFLUXDB_DB,
        INFLUX_ROOT_URL=str(influxdb.root_url),
    )
    process = main_process([], envargs)
    start_time = datetime.now()
    while not opcserver.has_subscriptions():
        elapsed = datetime.now() - start_time
        assert (
            elapsed.total_seconds() < 10
        ), "Timeout waiting for OPC-UA server to have subscriptions"
        time.sleep(1.0)
        assert process.poll() is None
    opcserver.change_node("recorded")
    lines: List[Dict[str, Any]] = []
    start_time = datetime.now()
    while not lines:
        elapsed = datetime.now() - start_time
        assert elapsed.total_seconds() < 10, "Timeout waiting InfluxDB to have series"
        lines = influxdb.query(InfluxDBQuery("GET", "SHOW SERIES", db=INFLUXDB_DB))
        time.sleep(1.0)
    measurements = [line["key"].split(",")[0] for line in lines]
    assert all(meas == "Recorded" for meas in measurements)
    lines = influxdb.query(
        InfluxDBQuery("GET", 'SELECT * FROM "Recorded"', db=INFLUXDB_DB)
    )
    assert len(lines) == 2, f"Got lines:\n{lines}"
    for i in range(2):
        line = next(line for line in lines if line["Recorded_index"] == str(i))
        assert line["Active"] == ["false", "true"][i]
        assert line["Age"] == ["67", "12"][i]
