import logging
import os
import time
from urllib.parse import urljoin

import requests
from mysql.connector import connect
from prometheus_client import start_http_server, Gauge, Counter

PORT = int(os.getenv('LISTENING_PORT', 8090))
UPDATE_TIME_IN_SEC = int(os.getenv('UPDATE_TIME_IN_SECS', 60))

ONCALL_URL = os.environ['ONCALL_URL']
PROM_URL = os.environ['PROM_URL']

MYSQL_HOST = os.environ['MYSQL_HOST']
MYSQL_PORT = os.environ['MYSQL_PORT']
MYSQL_USER = os.environ['MYSQL_USER']
MYSQL_PWD = os.environ['MYSQL_PWD']
MYSQL_DBNAME = os.environ['MYSQL_DBNAME']
MYSQL_SLA_DBNAME = os.getenv('MYSQL_SLA_DBNAME', 'sla_db')


def greater(): return lambda value, slo: value > slo


def less(): return lambda value, slo: value < slo


METRICS_AND_SLOS = [
    ('prober_get_team_by_name_success_total', 'increase(prober_get_team_by_name_success_total[1m])',
     10, less()),
    ('prober_get_team_by_name_fail_total', 'increase(prober_get_team_by_name_fail_total[1m])',
     0, greater()),
    ('prober_get_team_by_name_duration_seconds', 'prober_get_team_by_name_duration_seconds',
     0.1, greater()),

    ('prober_mysql_connection_success_total', 'increase(prober_mysql_connection_success_total[1m])',
     5, less()),
    ('prober_mysql_connection_fail_total', 'increase(prober_mysql_connection_fail_total[1m])',
     0, greater()),
    ('prober_mysql_connection_duration_seconds', 'prober_mysql_connection_duration_seconds',
     0.1, greater()),

    ('prober_web_page_availability_success_total', 'increase(prober_web_page_availability_success_total[1m])',
     5, less()),
    ('prober_web_page_availability_fail_total', 'increase(prober_web_page_availability_fail_total[1m])',
     0, greater()),
    ('prober_web_page_availability_duration_seconds', 'prober_web_page_availability_duration_seconds',
     0.1, greater())
]

duties_distribution_metric = Gauge('duty', 'On-call duty status', ['team', 'role'])

prober_get_team_by_name_total = Counter(
    "prober_get_team_by_name_total", "Total count of requests of getting team by name"
)
prober_get_team_by_name_success_total = Counter(
    "prober_get_team_by_name_success_total", "Total count of successful requests of getting team by name"
)
prober_get_team_by_name_fail_total = Counter(
    "prober_get_team_by_name_fail_total", "Total count of failed requests of getting team by name"
)
prober_get_team_by_name_duration_seconds = Gauge(
    "prober_get_team_by_name_duration_seconds", "Duration in seconds of executing request of getting team by name"
)

prober_mysql_connection_total = Counter(
    "prober_mysql_connection_total", "Total count of requests for probing MySQL connection"
)
prober_mysql_connection_success_total = Counter(
    "prober_mysql_connection_success_total", "Total count of successful requests for probing MySQL connection"
)
prober_mysql_connection_fail_total = Counter(
    "prober_mysql_connection_fail_total", "Total count of failed requests for probing MySQL connection"
)
prober_mysql_connection_duration_seconds = Gauge(
    "prober_mysql_connection_duration_seconds", "Duration in seconds of executing request for probing MySQL connection"
)

prober_web_page_availability_total = Counter(
    "prober_web_page_availability_total", "Total count of requests for probing web page availability"
)
prober_web_page_availability_success_total = Counter(
    "prober_web_page_availability_success_total", "Total count of successful requests for probing web page availability"
)
prober_web_page_availability_fail_total = Counter(
    "prober_web_page_availability_fail_total", "Total count of failed requests for probing web page availability"
)
prober_web_page_availability_duration_seconds = Gauge(
    "prober_web_page_availability_duration_seconds",
    "Duration in seconds of executing request for probing web page availability"
)


class OncallClient:
    def get_teams(self):
        try:
            response = requests.get(urljoin(ONCALL_URL, '/api/v0/teams'))
            return response.json()
        except Exception as e:
            logging.error(f'Failed to get teams: {e}')
            return ['Test Team']

    def get_team_by_name(self, name):
        prober_get_team_by_name_total.inc()
        duration = time.time()
        try:
            team = requests.get(urljoin(ONCALL_URL, '/api/v0/teams/' + name))
            duration = time.time() - duration
            prober_get_team_by_name_success_total.inc()
            prober_get_team_by_name_duration_seconds.set(duration)
            return team.json()
        except Exception as e:
            logging.error(f'Failed to get team: {e}')
            prober_get_team_by_name_fail_total.inc()
            duration = time.time() - duration
            prober_get_team_by_name_duration_seconds.set(duration)
            return None


class PrometheusClient:
    def __init__(self):
        self._QUERY_URL = PROM_URL + '/api/v1/query'

    def get_query_value(self, query):
        logging.debug(f'Requesting prometheus with query: {query}')
        response = requests.get(self._QUERY_URL, params={'query': query})
        try:
            if response.status_code != 200:
                raise ValueError(f'Failed to get metric value from prometheus: {response.text}')
            value = response.json()
            if value['status'] != 'success':
                raise ValueError(f'Failed to get metric value from prometheus: {value}')
            result = value['data']['result']
            if len(result) == 0:
                return 0
            metric = result[0]
            return float(metric['value'][1])
        except Exception as e:
            logging.error(f'Failed to get metric value from prometheus: {e}')
            return 0


class IndicatorsRepository:
    def __init__(self):
        self.connection = connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PWD,
        )
        self.migration_scripts = [
            f'create database if not exists {MYSQL_SLA_DBNAME}',
            f'use {MYSQL_SLA_DBNAME}',
            f"""
                create table if not exists indicators(
                    observed_at datetime not null default now(),
                    name varchar(255) not null,
                    slo float(4) not null,
                    value float(4) not null,
                    is_bad bool not null default false
                )
            """,
            'alter table indicators add index (observed_at)',
            'alter table indicators add index (name)'
        ]

        logging.info("Starting migration")
        cursor = self.connection.cursor(buffered=True)
        for script in self.migration_scripts:
            cursor.execute(script)
        logging.info("Migration finished successfully")


    def save_indicator(self, name, slo, value, is_bad):
        cursor = self.connection.cursor(buffered=True)
        script = 'insert into indicators (name, slo, value, is_bad) values (%s, %s, %s, %s)'
        values = (name, slo, value, int(is_bad))
        cursor.execute(script, values)
        self.connection.commit()

def update_metrics(oncall_client: OncallClient, indicators_repository: IndicatorsRepository,
                   prometheus_client: PrometheusClient):
    teams = oncall_client.get_teams()

    update_duties_for_teams_metric(oncall_client, teams)
    update_mysql_availability_metric()
    update_web_page_availability_metric()

    update_indicators(indicators_repository, prometheus_client)


def update_duties_for_teams_metric(oncall_client, teams):
    for team_name in teams:
        update_duties_for_team_metric(team_name, oncall_client)


def update_duties_for_team_metric(team_name, oncall_client):
    team = oncall_client.get_team_by_name(team_name)
    try:
        rosters = team['rosters'].values()
        primary_counter = 0
        secondary_counter = 0
        for roster in rosters:
            schedules = roster['schedules']
            for schedule in schedules:
                role = schedule['role']
                if role == 'primary':
                    primary_counter += 1
                elif role == 'secondary':
                    secondary_counter += 1
        duties_distribution_metric.labels(team=team_name, role='primary').set(primary_counter)
        duties_distribution_metric.labels(team=team_name, role='secondary').set(secondary_counter)
    except Exception as e:
        logging.error(f'Failed to update duties for team: {e}')


def update_mysql_availability_metric():
    prober_mysql_connection_total.inc()
    duration = time.time()
    try:
        with connect(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PWD,
                database=MYSQL_DBNAME
        ) as connection:
            cursor = connection.cursor(buffered=True)
            cursor.execute("SELECT @@global.read_only")
            connection.commit()
            duration = time.time() - duration
            prober_mysql_connection_success_total.inc()
            prober_mysql_connection_duration_seconds.set(duration)
            cursor.close()
    except Exception as e:
        prober_mysql_connection_fail_total.inc()
        logging.error(f"Something went wrong while connecting to db: {e}")
        duration = time.time() - duration
        prober_mysql_connection_duration_seconds.set(duration)


def update_web_page_availability_metric():
    prober_web_page_availability_total.inc()
    duration = time.time()
    try:
        result = requests.get(ONCALL_URL)
        duration = time.time() - duration
        if result.status_code == 200:
            prober_web_page_availability_success_total.inc()
            prober_web_page_availability_duration_seconds.set(duration)
        else:
            prober_web_page_availability_fail_total.inc()
    except Exception as e:
        prober_web_page_availability_fail_total.inc()
        logging.error(f'Something went wrong while executing main web page request: {e}')
        duration = time.time() - duration
        prober_web_page_availability_duration_seconds.set(duration)


def update_indicators(indicators_repository: IndicatorsRepository, prometheus_client: PrometheusClient):
    logging.debug('Processing metrics slos')
    for (name, query, slo, is_bad_fun) in METRICS_AND_SLOS:
        value = prometheus_client.get_query_value(query)
        indicators_repository.save_indicator(name=name, slo=slo, is_bad=is_bad_fun(value, slo), value=value)
    logging.debug('Processing finished successfully')


def main():
    logging.basicConfig(level=logging.DEBUG)
    oncall_client = OncallClient()
    prometheus_client = PrometheusClient()
    indicators_repository = IndicatorsRepository()

    logging.info('Starting server in port: ' + str(PORT))
    start_http_server(PORT)

    while True:
        update_metrics(oncall_client, indicators_repository, prometheus_client)
        time.sleep(UPDATE_TIME_IN_SEC)


if __name__ == '__main__':
    main()
