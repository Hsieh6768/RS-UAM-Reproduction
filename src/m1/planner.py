"""Request-scoped static planning. No simulator or live resource inputs."""
from collections import OrderedDict

from src.m1.config import M1Config
from src.m1.graph import TransferNetwork
from src.m1.pulse import materialize, reverse_pulse


class M1Planner:
    def __init__(self, config=None):
        self.config = config if config is not None else M1Config()
        self._version = None
        self._fixed = None
        self._plans = OrderedDict()

    def generate(self, request, immutable_network):
        cfg = self.config
        air = immutable_network.air
        # Only directed routes, nominal times and mapped stations enter M1.
        version = (immutable_network.road.version, tuple(sorted(air.station_nodes.items())),
                   tuple(sorted((key, r.origin_id, r.destination_id, r.nominal_time_s)
                                for key, r in air.routes.items())), cfg)
        if version != self._version:
            self._fixed = TransferNetwork(immutable_network, cfg)
            self._plans.clear()
            self._version = version
        key = (request.request_id, request.submitted_at, request.origin, request.destination,
               request.direct_road_time_s, request.pickup_tolerance_s, request.delay_tolerance_s)
        if key in self._plans:
            self._plans.move_to_end(key)
            return self._plans[key]
        graph = self._fixed.for_request(request)
        paths = reverse_pulse(request, graph, cfg)
        result = tuple(materialize(request, edges, cfg.walking_speed_mps, rank)
                       for rank, edges in enumerate(paths, start=1))
        self._plans[key] = result
        if len(self._plans) > cfg.cache_size:
            self._plans.popitem(last=False)
        return result
