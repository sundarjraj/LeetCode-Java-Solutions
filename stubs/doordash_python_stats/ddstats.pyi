import statsd
from typing import Dict, Optional
from typing_extensions import Protocol

Tags = Dict[str, str]

class DoorDashStatsD(Protocol):
    def timer(self, stat, rate=1, *, tags: Tags = {}): ...
    def timing(self, stat, delta, rrate=1, *, tags: Tags = {}): ...
    def incr(self, stat, count=1, rate=1, *, tags: Tags = {}): ...
    def decr(self, stat, count=1, rate=1, *, tags: Tags = {}): ...
    def gauge(self, stat, value, rate=1, *, tags: Tags = {}): ...
    def set(self, stat, value, rate=1, *, tags: Tags = {}): ...

class DoorStatsClient(statsd.StatsClient, DoorDashStatsD):
    def __init__(self, host="localhost", prefix=None, port=8125, fixed_tags=None): ...
    def init_tags(self, tags): ...

class DoorStatsProxyMultiServer(DoorDashStatsD):
    def __init__(self): ...
    def initialize(self, host="localhost", prefix=None, fixed_tags=None): ...
    def __getattr__(self, name): ...

doorstats: DoorStatsProxyMultiServer
doorstats_internal: DoorStatsProxyMultiServer
doorstats_global: DoorStatsProxyMultiServer
