# Copyright (c) 2019 Aiven, Helsinki, Finland. https://aiven.io/
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

import logging
import re

LOG = logging.getLogger(__name__)

SCRAPE_INTERVAL_SECONDS = 30
OVER_TIME_AGGREGATIONS = ["avg", "min", "max", "sum", "quantile", "stddev", "stdvar", "count"]
LABEL_COMPARISON_OPERATORS = ["=", "!=", "=~", "!~"]
LABEL_CONDITIONS_OPERATORS = ["<", ">", "<=", ">=", "="]
OPERATOR_REGEX_MAP = {"=": "=~", "!=": "!~"}
TIME_INTERVAL_REGEX = "[0-9]+[smhdwy][s]?"  # Match time(<<interval>>)
INVALID_PROMQL_METRIC_CHARACTERS = [".", "-"]
INTERVALS = [
    "$tinterval", "$interval", "$__interval", "$__rate_interval",
    "$groupByTime", "$timeWindow", "$groupbytime", "$groupBy", "$window", "$group_by",
    "$Resolution"
]

# When metrics type is histogram we usually need to use histgram_* functions. It's not easy to 
# identify if the metrics is a histogram from the name itself. Ieally we should call grafana 
# api/datasources/uid/yz_l5atIz/resources/api/v1/metadata endpoint to know the type of metrics
# which will produce more accurate results. This is a temporary measure to differentiate.
HISTOGRAM_METRICS_REGEX = [
    r".*_capacity_units",
    r".*_duration",
    r".*_alloc",
    r".*_goroutines",
    r".*_stats_used",
    r".*_stats_current_max",
    r".*_query_length",
    r"^router_connection_",
    r"^proc_go_stats_.*",
    r"routing_request_size",
    r".*_in_cents",
    r".*_latency*",
    r".*_alert_count",
    # r"error_count",
    r".*_completion_tokens",
    r"^cost_usage_",
    r".*_generated_quote_count",
    r".*_request_error_count",
    r".*_permanently_unprocessed_service_count",
    r".*_last_modified_time_stats_*",
    r"routing_request_size",
    r".*_input_price",
    r"^memcache_operation_.*",
    r"endpoint_*",
    r"budget_overserving_filterer_*",
    r"trigger_budget_overserving_upsell_*",
    r"result_quality_has_zero_services",
    r"result_quality_num_budget_overserved_services",
    r"budget_overserving_upsell_failure_failed_offer_count",
    r"missing_budget_overserving_upsell_pro_user_pk",
    r"instant_results_request_quality_has_project_pk",
    r"instant_results_filter_num_filtered_services",
    r"compound_quote_type_filter_num_*",
]

AGGREGATION_MAP = {
    "count": "count",
    "max": "max",
    "min": "min",
    "median": "avg",
    # Prometheus does not have a median function. It can do quantile(0,5...) on histograms only
    "mean": "avg",
    "non_negative_derivative": "rate",
    "non_negative_difference": "increase",
    "percentile": "quantile",  
    "stddev": "stddev",
    "sum": "sum",
    # Last isn't supported by Prometheus queries but if we drop the aggregation altogether there won't be
    # expected time based grouping so use average instead as an approximation
    # last_over_time is supported in Prometheus 2.25+ but not in M3 1.1 (at least)
    "last": "avg",

    # moving_average translated to avg_over_time
    "moving_average": "avg",
    "derivative": "rate",
    "difference": "delta"
}


AGGREGATION_PERCENTILE_MAP = {
    "max": 100,
    "mean": 50,
    "avg": 50,
    "min": 0,
}

# convert count(x) to sum(count_over_time(x[interval/__range]))
AGGREGATION_TOPLEVEL_MAP = {
    "count": "sum",
}

NONDEFAULT_METRIC_PREFIX_SCRAPE_INTERVAL_SECONDS = {
    "busy_metric_": 1,  # fictional example
}

# avg is shitty aggregation. So provide regexps for overrides, which
# we use if the original has 'avg' or n/a aggregation (but we do not
# override others).
METRIC_AGGREGATION_REGEXPS = [
    # These values we want to be as small as possible
    (".*_remaining_(percent_|)value", "min"),
    ("(disk|swap)_free", "min"),
    # ("cpu_usage_idle", "min"), # bad idea unless we applied to totalcpu only
    ("mem_available", "min"),
    # These ones as large as possible
    (".*_error_value", "max"),
    (".*_size(_value|$)", "max"),
    ("disk_used.*", "max"),
]


# Usually all the field names will have corresponding metric name in mimir, however certain fields are not
# mapped same way.
# For example: Metric name for ProcGoStats measurement with field name MemoryHeapAlloc is as expected converted
# to proc_go_stats_memory_heap_alloc but in mimir the Metric is called proc_go_stats_memory_alloc so we need
# this special handling in those cases
SPECIAL_FIELD_NAMES_MAPPING = {
    "MemoryHeapAlloc":"MemoryAlloc",
}

def _duration_to_seconds(v: str) -> int:
    v = v.lower()
    if v.endswith("s"):
        return int(v[:-1])
    if v.endswith("m"):
        return int(v[:-1]) * 60
    if v.endswith("h"):
        return int(v[:-1]) * 3600
    if v.endswith("d"):
        return int(v[:-1]) * 86400
    raise NotImplementedError(f"unknown duration {v}")


def _seconds_to_duration(seconds: int) -> str:
    mins, div = divmod(seconds, 60)
    if div or not mins:
        return f"{seconds}s"
    hours, div = divmod(mins, 60)
    if div or not hours:
        return f"{mins}m"
    return f"{hours}h"


def _convert_to_snake_case(name):
    # Convert CamelCase to snake_case
    # This regex finds a sequence of uppercase letters and inserts an underscore before it.
    name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    name = name.replace("::tag", "")
    return name.lower()


class GroupBy(NamedTuple):
    group_by: str
    over_time: str
    fills: List[str]


class ConvertError(Exception):
    pass


class InfluxQLToM3DashboardConverter:
    def __init__(
            self,
            *,
            datasource_map: Optional[Dict[str, str]] = None,
            alert_notifications_map: Optional[Dict[int, str]] = None,
            alert_notifications_uid_map: Optional[Dict[str, str]] = None,
            scrape_interval: int = SCRAPE_INTERVAL_SECONDS,
            log_level=logging.INFO,
            default_measurement: Optional[bool] = None,
    ) -> None:
        logging.getLogger(__name__).setLevel(level=log_level)

        self.datasource_map = datasource_map
        self.alert_notifications_map = alert_notifications_map
        self.alert_notifications_uid_map = alert_notifications_uid_map
        self.scrape_interval = scrape_interval
        self.group_by_labels = None
        self.metric_to_objects = {}  # dict of: metric -> {panel,dashboard title} to avoid iterating over all panels
        self.current_dashboard = ''
        self.default_measurement = default_measurement

        # (Flag, metric and parent json of label_values string) object  to determine if the metric in the label_values requires replacement. i.e:
        # label_values(net,host) ---> label_values(net__bytes_recv,host). Any metric with the same service
        # is acceptable.
        self._get_rep_metric = (False, None, None)

    def get_metric_field_from_select(self, select):
        field = None
        for item in select[0]:
            if item['type'] == 'field':
                field = item['params'][0]
                break
        return field

    def _replace_invalid_metric_characters(self, metric) -> str:
        for character in INVALID_PROMQL_METRIC_CHARACTERS:
            metric = metric.replace(character, "_")
        return metric

    def add_to_metric_and_object_list(self, old_targets, new_targets):
        for i, old_target in enumerate(old_targets):
            if old_target.get("rawQuery"):
                metric_name = self.get_metric_name_snake_case(old_target.get("query"))
            elif old_target.get("measurement"):
                metric_name = old_target.get("measurement") + "_"
                # replace . and - with _ for the metric names. (promql valid metric name contains _ only)
                metric_name += self.get_metric_field_from_select(old_target['select'])
                # Replace camelCase in the measurement name with snake_case
                metric_name = _convert_to_snake_case(metric_name)
                metric_name = self._replace_invalid_metric_characters(metric_name)
            else:
                # No metric to add in this target.
                continue

            try:
                self.metric_to_objects[metric_name][self.current_dashboard].append(new_targets[i])
            except KeyError:
                if not self.metric_to_objects.get(metric_name):
                    self.metric_to_objects[metric_name] = {}
                self.metric_to_objects[metric_name][self.current_dashboard] = []
            self.metric_to_objects[metric_name][self.current_dashboard].append(new_targets[i])

    def get_metric_aggregation(self, metric_name):
        for metric_re, fun in METRIC_AGGREGATION_REGEXPS:
            if re.match(metric_re, metric_name) is not None:
                return fun
        return "avg"

    def convert_alerts(self, alert: dict, panel_title) -> None:
        if not self.alert_notifications_map or not self.alert_notifications_uid_map:
            del alert["notifications"]
            LOG.warning(
                f"No alert notification mapping defined, deleting alert notifications array in panel: {panel_title}")
            return
        alert["name"] = "{name} (M3)".format(name=alert["name"])
        notifications: List[dict] = []
        for notification in alert["notifications"]:
            n_id = notification.get("id")
            new_uid = self.alert_notifications_map.get(n_id) if n_id else None
            if new_uid is None:
                n_uid = notification["uid"]
                try:
                    new_uid = self.alert_notifications_map.get(int(n_uid))
                except ValueError:
                    new_uid = self.alert_notifications_uid_map.get(n_uid)
            if new_uid is None:
                raise ValueError(f"Alert mapping for notification {notification} missing")
            new_notification = {"uid": new_uid}
            if new_notification not in notifications:
                notifications.append(new_notification)
        alert["notifications"] = notifications

    def update_ds_to_mimir(self, datasource):
        if not self.datasource_map:
            return datasource
        if isinstance(datasource, dict) and datasource.get("type") == "influxdb":
            influx_ds_uid = datasource.get("uid")
            mimir_ds_uid  = self.datasource_map.get(influx_ds_uid)
            if mimir_ds_uid:
                datasource["uid"] = mimir_ds_uid
                datasource["type"] = "prometheus"
                LOG.info(f'Updated datasource UID from influx {influx_ds_uid} to mimir {mimir_ds_uid} : ')
            return datasource
        else:
            LOG.info(f"Datasource type is mixed for {datasource}, update datasource at targets level")
            return datasource

    def convert_templating(self, template: dict) -> None:
        for item in template["list"]:
            if item["type"] == "query":
                datasource = item["datasource"]
                if datasource.get("type") == "prometheus":
                    LOG.info(f"Skipping item query for prometheus datasource {datasource}")
                    continue
                item["sort"] = 3  # Numerical(asc)
                query = item["query"]
                # Uncommon case where they query is a dict
                if isinstance(query, dict) and query.get('query'):
                    query = query['query']
                query = self._replace_invalid_metric_characters(query)
                pattern = r"show tag values from (?P<tag>.+?) with key\s?=\s?['|\"](?P<key>.+?)['|\"]( where service_type\s?=\s?['|\"](?P<service_type>.+?)['|\"] and time > now\(\) - (?P<interval>.+?$))?"
                m = re.search(pattern, query, re.IGNORECASE)
                if m is None:
                    pattern = r"SHOW TAG VALUES FROM ['|\"]?(?P<tag>.+?)['|\"]? WITH KEY\s?=\s?['|\"](?P<key>.+?)['|\"]"
                    m = re.search(pattern, query, re.IGNORECASE) 
                if m is None:
                    # check dashboard for alternative definition
                    m = self.extract_key_all_case(query)
                if m is None:
                    LOG.warning(f"Unable to find tag values or key from {query!r} ignoring")
                    continue
                # Update datasource to mimir only if the query can be updated to use mimir datasource
                item["datasource"] = self.update_ds_to_mimir(datasource)

                groupdict = m.groupdict()
                # Metric in mimir is always in snake_case and append _count to the metric name
                metric = groupdict.get("tag")
                key = groupdict["key"]
                if metric:
                    metric = _convert_to_snake_case(metric) + "_count"
                if key:
                    key = _convert_to_snake_case(key)

                service_type = groupdict.get("service_type")
                interval = groupdict.get("interval")

                if key in ("host", "service_id") and service_type and interval and metric:
                    new_query = (
                        f'query_result(avg by({key})(avg_over_time({metric}{{service_type="{service_type}"}}[{interval}])))'
                    )
                    item["regex"] = f'/{key}="(.*?)"/'
                elif metric:
                    new_query = f"label_values({metric},{key})"
                else:
                    new_query = f"label_values({key})"
                item["query"] = new_query.replace("cpu", "cpu_usage_idle")
                if metric and len(metric.split("_")) < 3:
                    self._get_rep_metric = (True, metric, item)
            elif item["type"] in {"custom", "interval", "datasource"}:
                pass
            else:
                raise ValueError(f"Unknown template type for item {item}")

    def extract_key_all_case(self, query):
        pattern = r"SHOW TAG VALUES WITH KEY\s?=\s?(?:['\"])?(?P<key>[a-zA-Z0-9_]+)(?:['\"])?"
        m = re.match(pattern, query, re.IGNORECASE)
        return m

    def get_group_by_tags(self, group_bys : str) -> List[str]:
        group_bys = group_bys.replace("::tag", "").replace('"', "")
        return group_bys.split(",")

    def is_rate_aggregation(self, aggregations: List[str], metric_name: str) -> bool:
        if any(agg in aggregations for agg in ["rate", "quantile", "mean", "avg"]):
            # mean - we generally use histogram_quantile(0.5, sum by(rate..)) for mean
            return True
        if any(agg in aggregations for agg in ["max", "min"]):
            for metric_re in HISTOGRAM_METRICS_REGEX:
                if re.match(metric_re, metric_name) is not None:
                    return True
        return False

    def format_expression(
            self,
            *,
            divide_by_self: bool,
            metric_name: str,
            aggregations: List[str],
            label_filters: str,
            group_by: Optional[GroupBy],
            modifications: List[str],
            conditions,
            percentile_value: Optional[str] = None,
            offset_in_seconds: Optional[int] = None,
    ) -> Tuple[str, List[str], List[str]]:
        if not isinstance(conditions, list):
            conditions = [conditions]

        metric_name = self._replace_invalid_metric_characters(metric_name)
        expressions = []
        over_times = []
        for condition in conditions:
            over_time_aggregations = set()
            over_time = group_by.over_time if group_by else None
            aggregation = aggregations[-1] if aggregations else ""
            group_by_tags = self.get_group_by_tags(group_by.group_by) if group_by and group_by.group_by else []
            orig_aggregation = ""

            if group_by:
                if not over_time:
                    if "count" in aggregations:
                        over_time = "$__range"
                else:
                    for aggregation in aggregations:
                        if aggregation in OVER_TIME_AGGREGATIONS:
                            if self.is_rate_aggregation(aggregations, metric_name):
                                over_time_aggregations.add("rate")
                                percentile_value = AGGREGATION_PERCENTILE_MAP.get(aggregation, percentile_value)
                                # For over time aggregation of rate type we need to use sum
                                # and wrap it with histogram_quantile(percentile, ...).
                                orig_aggregation = aggregation
                                aggregation = "sum"
                            elif any(agg in aggregations for agg in ["increase", "count", "sum"]):
                                over_time_aggregations.add("increase")
                                orig_aggregation = aggregation
                                aggregation = "sum"
                            elif any(agg in aggregations for agg in ["max", "min"]):
                                # Over time aggregation are part of inner expression so it should
                                # fine to add them even if there are group_by tags
                                # For example max by (app)(max_over_time(metric)[period]) is valid
                                over_time_aggregations.add(f"{aggregation}_over_time")
                            elif not group_by_tags:
                                over_time_aggregations.add(f"{aggregation}_over_time")
            if divide_by_self:
                if not condition:
                    condition = "!= 0"
                # conditions are of 'op value' format. We convert them to
                # op bool value
                condition = condition.replace(" ", " bool ", 1)
            if condition == "> 0" and aggregation == "sum":
                condition = ""
                # Extra zero results from m3 are fine
            if aggregation:
                if aggregation in {"rate", "increase"} and not over_time_aggregations:
                    over_time_aggregations.add(aggregation)
                    # Default to 'avg' aggregation + $__rate_interval in this case
                    aggregation = self.get_metric_aggregation(metric_name)
                    if not over_time:
                        over_time = "$__rate_interval"
                # Below works sometimes but doesn't work for other graphs. Review what is the correct way
                # to determine if we should add $__rate_interval?
                if aggregation in {"max", "min"}:
                    if any(otg in over_time_aggregations for otg in ["max_over_time", "min_over_time"]):
                        if not re.match(TIME_INTERVAL_REGEX, over_time, re.IGNORECASE) and over_time not in INTERVALS:
                            over_time = "$__rate_interval"
                if any(otg in over_time_aggregations for otg in ["rate"]):
                    if not re.match(TIME_INTERVAL_REGEX, over_time, re.IGNORECASE) and over_time not in INTERVALS:
                        over_time = "$__rate_interval"

            # Handle group_by tags for PromQL
            aggregation_str = ""
            if group_by_tags:
                tags_str = ", ".join(group_by_tags)
                tags_str = _convert_to_snake_case(tags_str)
                aggregation_str = f"{aggregation} by {tags_str} "
            else:
                aggregation_str = aggregation

            inner_expr = ""
            if over_time_aggregations:
                inner_expr += "{}(".format(" ".join(over_time_aggregations))
            else:
                inner_expr = "("

            metrics_label_exp = metric_name
            if label_filters:
                metrics_label_exp += f"{{{label_filters}}}"

            inner_expr += metrics_label_exp
            default_expr = ""

            if over_time:
                scrape_interval = self.scrape_interval
                for metric_prefix, scrape_interval_override in NONDEFAULT_METRIC_PREFIX_SCRAPE_INTERVAL_SECONDS.items():
                    if metric_name.startswith(metric_prefix):
                        scrape_interval = scrape_interval_override
                if "rate" in aggregations or "increase" in aggregations:
                    # With default scrape interval, we just assume using __rate_interval is enough
                    if over_time in {"$__rate_interval", "$__interval"} and scrape_interval == self.scrape_interval:
                        over_time = "$__rate_interval"
                    else:
                        over_times.append(_seconds_to_duration(scrape_interval * 4))
                else:
                    over_times.append(_seconds_to_duration(scrape_interval))

                if over_time_aggregations:
                    # interval_replace = re.sub(TIME_INTERVAL_REGEX, "$__interval", over_time)
                    # print(f"interval_replace: {interval_replace} when over_time: {over_time}")
                    inner_expr += f"[{over_time}]"
                    if offset_in_seconds:
                        inner_expr += f" offset {offset_in_seconds}s"

                over_times.append(over_time)

            if over_time_aggregations:
                inner_expr += f"){condition}"
            else:
                inner_expr += "{conditions}){modifications}".format(
                    modifications=" ".join(modifications), conditions=condition
                )

            # Add Default expression assuming the code is not setting the default value.
            # Add comment about default expression.
            if (aggregation == "sum" and
                    any(otg in over_time_aggregations for otg in ["increase", "rate"])):
                histogram = any(re.match(metric_re, metric_name) for metric_re in HISTOGRAM_METRICS_REGEX)
                if orig_aggregation == "max" or orig_aggregation == "min":
                    # For max_over_time and min_over_time not using any default expression.
                    default_expr = ""
                else:
                    default_expr = f"({metrics_label_exp} unless {metrics_label_exp} offset {over_time}) or {metrics_label_exp} offset {over_time} * 0"

            expr = ""
            if self.default_measurement and default_expr:
                expr += f"{aggregation_str} ({inner_expr}) \n # Optional - Default exp uncomment below if the code is not initializing default value. \n  # + {aggregation_str} ({default_expr}) \n"
            else:
                expr += f"{aggregation_str} ({inner_expr})"

            if over_time_aggregations:
                if "rate" in over_time_aggregations and "sum" == aggregation:
                    expr = f"histogram_quantile({percentile_value}/100, {expr})"
                if "increase" in over_time_aggregations and "sum" == aggregation:
                    for metric_re in HISTOGRAM_METRICS_REGEX:
                        if re.match(metric_re, metric_name) is not None:
                            if orig_aggregation == "sum":
                                expr = f"histogram_sum({expr})"
                            if orig_aggregation == "count":
                                expr = f"histogram_count({expr})"
                            break
                expr += "{modifications}".format(modifications=" ".join(modifications))

            expressions.append(expr.replace("'", "'"))
        fills = group_by.fills if group_by else []
        return " or ".join(expressions), over_times, fills

    def get_metric_name(self, query: str) -> Tuple[str, str]:
        # udpated_query = query.replace("-", "_")
        part1 = re.search(r'from\s+"(\S+)"\s+where', query, re.IGNORECASE)
        if part1 is None:
            raise ValueError(f"Unable to find metric name in {query}")
        # Added |. after word selection for specific dashboard with a . in the metric name
        part2 = re.search(r'(\("|\(|\")([\w.]+)("\)|\)|")', query, re.IGNORECASE)
        if part2 is None:
            raise ValueError(f"Unable to find (single) metric key in {query}")
        series_name = part1.group(1)
        # Check if part2 is "duration" and if it is used with count
        if part2.group(2).lower() == "duration" and re.search(r'count\(\s*"?duration"?\s*\)', query, re.IGNORECASE):
            return series_name, "count"

        field_name = part2.group(1) if part2.group(1) not in {'"', "(", '("'} else part2.group(2)
        if field_name in SPECIAL_FIELD_NAMES_MAPPING:
            field_name = SPECIAL_FIELD_NAMES_MAPPING[field_name]
        return series_name, field_name

    # Returns metric name as series_field_name in snake case.
    def get_metric_name_snake_case(self, query: str) -> str:
        series_name, field_name = self.get_metric_name(query)
        field_name = field_name.replace(".", "_")  # Specific use case for metric names with . as a seperator
        metric_name = "{}_{}".format(series_name, field_name)
        # Replace camelCase in the measurement name with snake_case
        metric_name = _convert_to_snake_case(metric_name)
        return self._replace_invalid_metric_characters(metric_name)


    def get_aggregations(self, query: str) -> List[str]:
        part1 = re.search(r"select (.*) from", query, re.IGNORECASE)
        if part1 is None:
            raise ValueError(f"Unable to find aggregations in {query}")
        part2 = part1.group(1).split("(")
        part3 = [AGGREGATION_MAP[item] for item in part2 if item in AGGREGATION_MAP]
        return part3


    def get_percentile(self, query: str, aggregations: List[str]) -> Optional[str]:
        if aggregations != ["quantile"]:
            return None
        match = re.search(r'percentile\(\s*".+?"\s*,\s*([\w$]+)\s*\)', query, re.IGNORECASE)
        if match:
            return str(match.group(1))
        return None

    def get_label_filters(self, query: str, *, field_name: str) -> Tuple[str, str]:
        """Converts any equals, not equals, like and not like InfluxQL where conditions into corresponding Prometheus conditions.

        E.g. "abc" = def AND foo =~ /bar$/ => abc='def',foo=~'.*bar'
        """
        label_filters = set()

        # Bit hacky regexp handling trick:
        #
        # Replace (within parentheses) set of ORs with equalities with
        # single regexp
        single_eq_re = r""""(\S+)"\s*=\s*'(\S+)'"""

        def _replace(m):
            original_string = m.group(1)
            results = re.findall(single_eq_re, m.group(1))
            keys = set(r[0] for r in results)
            if len(keys) == 1:
                # only r[1]s differ -> we can create regexp to match them
                key = next(iter(keys))
                values = "|".join(r[1] for r in results)
                label_filters.add(f'{_convert_to_snake_case(key)}=~"{values}"')
                return ""
            return original_string

        query = re.sub(f"(\\({single_eq_re}\\s+(OR\\s+{single_eq_re})*\\))", _replace, query, re.IGNORECASE)

        for operator in LABEL_COMPARISON_OPERATORS:
            if operator in {"!~", "=~"}:
                # Regex matches are of the form "field" =~ /regex/. regex could contain escaped forward
                # slash so only end when finding / that isn't preceded by uneven number of backslashes.
                # Actual content matching is non-greedy as there may be forward slashes later in content.
                # Field name may or may not be double quoted. If it isn't, it should be simple alphanumeric
                # string.
                regex = r'(\w+?|"\S+?")\s*{}\s*/(.*?)(?<!\\)(?:\\{{2}})*\$?/'.format(operator)
                for (key, value) in re.findall(regex, query):
                    key = key.replace("::tag", "").lstrip('"').rstrip('"').replace('-', "_")

                    if key == field_name:
                        continue
                    # InfluxQL regexes are search-like (match anywhere) while Prometheus does exact
                    # matches. Need to convert patterns accordingly
                    if value.startswith("^"):
                        value = value[1:]
                    elif not value.startswith(".*"):
                        value = f".*{value}"
                    if value.endswith("$"):
                        value = value[:-1]
                    elif not value.endswith(".*"):
                        value = f"{value}.*"
                    # Forward slashes in the query have been escaped, strip the escapes for Prometheus
                    value = value.replace("\\/", "/")
                    # Regex escapes need to be escaped themself
                    value = value.replace("\\", "\\\\")
                    # Need to escape single quotes
                    value = value.replace("'", "\\'")
                    label_filters.add(f'{_convert_to_snake_case(key)}{operator}"{value}"')
            else:
                # Non regex matches are of the form "field" = foobar or "field" = 'foo-bar'. Look for
                # both forms separately. Field name may or may not be double quoted. If it isn't, it
                # should be simple alphanumeric string.
                regex = r'(\w+?|"\S+?")\s*{}\s*\'(.*?)(?<!\\)(?:\\{{2}})*\''.format(operator)
                # regex = r'((?:"\w+"|[a-zA-Z_]\w*)::\w+?)\s*{}\s*\'(.*?)(?<!\\)(?:\\{{2}})*\''.format(operator)

                # Dictionary to collect values for each key. This is needed so we could
                # Or the values with same key.
                key_values_map = {}

                def _escape_backslashes(s):
                    # There's stuff like "aiven\.prune" floating
                    # around in exact matches, and m3 doesn't like
                    # escapes in the normal strings that it does not
                    # support (it only supports \n\r or something I
                    # suppose)
                    return s.replace("\\", "\\\\")

                for (key, value) in re.findall(regex, query):
                    key = key.replace("::tag", "").lstrip('"').rstrip('"')
                    if key == field_name:
                        continue
                    value = _escape_backslashes(value)
                    key_values_map.setdefault(key, []).append(value)
                # Non-quoted value variant is terminated either by whitespace or closing parenthesis
                regex = r'(\w+?|"\S+?")\s*{}\s*([^\s\'~].*?)(?:\)|\s)'.format(operator)
                for (key, value) in re.findall(regex, query):
                    LOG.info(f"key = {key} value = {value}")
                    key = key.replace("::tag", "").lstrip('"').rstrip('"')
                    if key == field_name:
                        continue
                    value = _escape_backslashes(value)
                    key_values_map.setdefault(key, []).append(value) 

                # Generate label filters
                for key, values in key_values_map.items():
                    if len(values) > 1:
                        operator = OPERATOR_REGEX_MAP.get(operator, operator)
                        # Multiple values for the same key, combine with `|` using regex operator
                        combined_values = "|".join(values)
                        label_filters.add(f'{_convert_to_snake_case(key)}{operator}"{combined_values}"')
                    else:
                        # Single value for the key, use `=` or the specified operator
                        label_filters.add(f'{_convert_to_snake_case(key)}{operator}"{values[0]}"')


        return query, ",".join(sorted(label_filters))

    def get_conditions(self, query: str, *, field_name: str) -> Union[str, List[str]]:
        conditions = []
        for operator in LABEL_CONDITIONS_OPERATORS:
            for (key, value) in re.findall(r'(\w+?|"\S+?")\s*{}\s*(-?\w+(?:\.?\d+)?)'.format(operator), query):
                key = key.strip('"')
                # Prometheus queries don't support filtering by values that are not actually selected
                if key != field_name:
                    if operator == "=":
                        continue
                    raise ValueError(f"Query {query!r} has condition that does not match select field, cannot convert")
                if operator == "=":
                    operator = "=="
                conditions.append(f"{operator} {value}")
        # To make this work reliably we should actually parse the query but in our case there are only couple
        # of queries that use OR and all of those are simple queries with nothing but the OR. For Prometheus
        # queries OR needs to generate multiple different queries, otherwise the conditions are treated as AND
        if " OR " in query and conditions:
            return conditions
        else:
            return " ".join(conditions)

    def does_divide_by_self(self, query: str) -> bool:
        """Returns True if the query contains select like 'SELECT max("foo") / max("foo") FROM somwhere'."""
        return bool(re.search(r"select\s+(\S+?)\s*/\s*\1\s+from ", query, re.IGNORECASE))

    def get_modifications(self, query: str) -> List[str]:
        modifications = []
        # Basic arithmetic operations where the other side is a number
        part1 = re.search(r'[")](\s*[*/+\-]\s*-?[\d]+?(?:\.\d+?)?) from', query, re.IGNORECASE)
        if part1:
            modifications.append(part1.group(1))
        return modifications

    def get_group_by(self, query: str) -> Optional[GroupBy]:
        self.group_by_labels = None
        fills = []
        group_by = []
        time_val = ""
        # groups = query.lower().split("group by")
        groups = re.split("group by", query, flags=re.IGNORECASE)

        if len(groups) < 2:
            return None
        groups = groups[1].split(" ")
        for outer_group in groups:
            inner_groups = outer_group.split(",")
            for group in inner_groups:
                if not group:
                    continue
                m = re.match(r"(?i)fill\((.*)\)", group)
                if m is not None:
                    fills.append(m.group(1))
                    continue
                if "time" in group:
                    m = re.search("\\((.*)\\)", group)
                    if m is None:
                        raise ValueError(f"Unparseable group by statement: {group}")
                    time_val = m.group(1).replace("$_interval", "$__interval").replace("auto", "$__interval").replace(
                        "$interval", "$__interval")
                    continue
                group_by.append(group.replace(",", "").replace('"', ""))
        self.group_by_labels = group_by
        group_by_str = "({})".format(",".join(group_by)) if group_by else ""
        return GroupBy(group_by=group_by_str, over_time=time_val, fills=fills)

    def convert_expression(self, query: str, offset_in_sec:int) -> Tuple[str, List[str], List[str]]:
        series_name, field_name = self.get_metric_name(query)
        field_name = field_name.replace(".", "_")  # Specific use case for metric names with . as a seperator
        metric_name = "{}_{}".format(series_name, field_name)
        metric_name = _convert_to_snake_case(metric_name)
        aggregations = self.get_aggregations(query)
        if aggregations == ["avg"]:
            default_aggregation = self.get_metric_aggregation(metric_name)
            aggregations = [default_aggregation]

        percentile_value = self.get_percentile(query, aggregations)
        query, label_filters = self.get_label_filters(query, field_name=field_name)

        modifications = self.get_modifications(query)
        group_by = self.get_group_by(query)
        if not aggregations and group_by and group_by.group_by:
            # We need to have *some* aggregation to group by
            aggregations = [self.get_metric_aggregation(metric_name)]
            if aggregations == ["avg"]:
                LOG.debug("Using default aggregation of avg for %r", query)
        conditions = self.get_conditions(query, field_name=field_name)
        divide_by_self = self.does_divide_by_self(query)
        if divide_by_self and (conditions or modifications):
            raise ValueError(f"Unsupported query {query}, divide by self combined with conditions or modifications")

        return self.format_expression(
            divide_by_self=divide_by_self,
            metric_name=metric_name,
            aggregations=aggregations,
            label_filters=label_filters,
            group_by=group_by,
            modifications=modifications,
            conditions=conditions,
            percentile_value=percentile_value,
            offset_in_seconds=offset_in_sec,
        )

    def convert_subquery(self, query: str, offset_in_seconds:Optional[int]) -> Tuple[str, List[str], List[str]]:
        # In-InfluxDB downsampling case:
        #
        # SUM .. FROM ( subquery GROUP BY X, Z ) GROUP BY X
        # =~
        # PromQL:
        #
        # sum by (X)(converted subquery without outer aggregation)
        m = re.match(
            r"""
        \s*SELECT\s+SUM\("(?P<outer_key>[a-z]+)"\)\s*
        FROM\s*\(
          (?P<inner1>\s*SELECT\s*[^\s]+\s*)
          (AS\s\S+\s+)?
          (?P<inner2>.*)\s+
          GROUP\s+BY.*
        \)\s*
        (?P<outer_group>GROUP\sBY\s.*)
        """,
            query,
            re.VERBOSE | re.IGNORECASE,
        )
        if m is not None:
            d = m.groupdict()
            inner1 = d["inner1"]
            inner2 = d["inner2"]
            outer_group = d["outer_group"]
            subquery = f"{inner1} {inner2} {outer_group}"
            expr, over_times, fills = self.convert_expression(subquery, offset_in_seconds)

            # This could be neater if we actually passed the desired outer
            # function to convert/format expression.
            if over_times:
                m = re.match(r"\S+ by (\((.*))", expr)
                if m is not None:
                    rest = m.group(1).strip()
                    expr = f"sum by {rest}"
                    return expr, over_times, fills
        raise ValueError(f"Unsupported subquery in {query!r}")

    def convert_special_or_expression(self, query, offset_in_sec:Optional[int]):
        # Handling for 'special' expressions that we hardcode.
        #
        # We should have real parser but oh well.
        m = re.match(r"\s*SELECT (.*) FROM (.*)", query, re.IGNORECASE)
        if m is None:
            raise ValueError(f"Unable to parse initial SELECT part: {query}")
        return_part, from_tail = m.groups()
        return_part = re.sub(r"\s+", " ", return_part).strip()
        if return_part == 'mean("num_fds") / mean("rlimit_num_fds_soft") * 100':
            r = self.convert_expression(f'SELECT mean("num_fds") FROM {from_tail}', offset_in_sec)
            exp_re = r"avg_over_time\(procstat_num_fds({[^}]+}\[[^\]]+\])\)"
            assert re.search(exp_re, r[0]) is not None, f"Unexpected input: {r[0]}"

            def _sub(m):
                s = m.group(1)
                return f"avg_over_time(procstat_num_fds{s}) / avg_over_time(procstat_rlimit_num_fds_soft{s}) * 100"

            new_q = re.sub(exp_re, _sub, r[0]).strip()
            # If rlimit_num_fds_soft doesn't happen to come with
            # num_fds (it happens sometimes, prom or m3 weirdness?), result
            # is Inf. So we filter it explicitly in the end,
            # pretending there is no datapoint at all even if num_fds
            # had one.
            return (f"{new_q} != Inf", r[1], r[2])

        m = re.match(r"(\d+)\s?([-+*/])\s?(.*)$", return_part)
        if m is not None:
            # Redis panel specialty; this math doesn't really work as is
            constant, op, expr = m.groups()
            r = self.convert_expression(f"SELECT {expr} FROM {from_tail}", offset_in_sec)
            new_q = r[0].strip()
            return (f"{constant} {op} {new_q}", r[1], r[2])

        return self.convert_expression(query, offset_in_sec)

    def convert_query(self, query: str, target: dict, legend: str, offset_in_sec:Optional[int]) -> Tuple[dict, List[str], List[str]]:
        if "<>" in query:
            raise ValueError(f"Unexpected <> found from query {query!r}, use != instead")
        if "(*)" in query:
            raise ValueError("Unsupported (*) query in {query!r}")
        if "from (" in query.lower():
            expr, over_times, fills = self.convert_subquery(query, offset_in_sec)
        else:
            expr, over_times, fills = self.convert_special_or_expression(query, offset_in_sec)
        group_by = self.get_group_by(query)
        min_step = group_by.over_time if group_by else None
        fmt = target.get("resultFormat", "time_series")
        if not legend:
            legend = self.get_metric_name_snake_case(query)

        new_target = {
            "expr": expr,
            "format": fmt,
            "instant": fmt == "table",
            "intervalFactor": 1,
            "refId": target["refId"],
            "legendFormat": legend,
    	    "interval": min_step,
        }

        if "hide" in target:
            new_target["hide"] = target["hide"]
        if "datasource" in target:
            new_target["datasource"] = target["datasource"]

        return new_target, over_times, fills

    def convert_to_query(self, target: dict, legend: str) -> [Tuple[dict, List[str], List[str]]]:
        value = ""
        select_what_list = []
        modifications: List[str] = []
        if target.get('select') is None:
            raise ValueError(f'Dashboard {self.current_dashboard} is invalid, missing select field in target')

        for select in target["select"]:
            select_what = ""
            for item in select:
                item_type = item["type"]
                if item_type == "field":
                    value = item["params"][0]
                elif item_type == "math":
                    modifications.extend(item["params"])
                elif item_type in AGGREGATION_MAP:
                    param_str = ""
                    if item["params"]:
                        param_str = ", {}".format(item["params"][0])
                    if select_what:
                        select_what = f"{item_type}({select_what}{param_str})"
                    elif item_type:
                        # else:
                        # Condition to filter unnecessary aggregation in m3
                        select_what = f'{item_type}("{value}"{param_str})'
                elif item_type == "alias":
                    # This is only used in the Maps dashboard. This is actually relevant but the map itself is
                    # of questionable value so just ignore the issue for now
                    LOG.debug("Dropping unsupported alias: %r", item)
                elif item_type == "distinct":
                    LOG.debug("Dropping unsupported item type: distinct")
                else:
                    raise ValueError(f"Unknown item type {item_type!r} in {target!r}")
            select_what =  f'"{value}"' if select_what == "" else select_what
            select_what_list.append(select_what)

        where_items = ["$timeFilter"]
        target_tags = target.get("tags")
        if target_tags and any(tag.get("condition") == "OR" for tag in target_tags):
            # Prometheus queries don't directly support label selection using OR. We can convert OR into
            # regular expression but only if all tags have the same key
            keys = [tag["key"].replace("::tag", "") for tag in target["tags"]]
            if len(set(keys)) != 1:
                raise ValueError(f"Unsupported OR for tags with different keys: {keys!r}")
            # We could support operators other than = but that would make the regex generation a bit difficult
            if not all(tag.get("operator") == "=" for tag in target["tags"]):
                raise ValueError(f"Unsupported operator in OR tag: {target!r}")
            values = [re.escape(tag["value"]) for tag in target["tags"]]
            where_items.append('"{key}"=~/^{value}$/'.format(key=keys[0], value="|".join(values)))
        elif target_tags:
            for tag in target_tags:
                operator = tag["operator"]
                # Our query parsing doesn't handle <> correctly, use != instead
                if operator == "<>":
                    operator = "!="
                where_items.append(
                    '"{key}"{operator}{value}'.format(key=tag["key"], operator=operator, value=tag["value"]))
        where = "({})".format(" AND ".join(where_items))

        group_by_items = []
        for group in target["groupBy"]:
            if group["type"] == "time":
                group_by_items.append("{}({})".format(group["type"], group["params"][0]))
            elif group["type"] == "tag":
                group_by_items.append('"{}"'.format(group["params"][0]))
            elif group["type"] == "fill":
                # The "Null value: connected" visualization option should be used instead of using fill query
                continue
            else:
                group_type = group["type"]
                raise ValueError(f"Unknown group type {group_type} in {target}")

        group_by = " GROUP BY {}".format(",".join(group_by_items)) if group_by_items else ""
        if not target.get('measurement'):
            raise ValueError(f'Missing measurement field for target, in dashboard: {self.current_dashboard}')

        offset_in_sec = target.get("offset")

        result = []
        start_ref_id = target["refId"]
        for index, select_what in enumerate(select_what_list):
            # Each measurement in influx is a separate time series in mimir. So if the influx query has select for
            # different fields then we need to create a separate measurement for each field.
            query = 'SELECT {select_what}{modifications} FROM "{measurement}" WHERE {where}{group_by}'.format(
                select_what=select_what,
                modifications=" ".join(modifications),
                measurement=target["measurement"],
                where=where,
                group_by=group_by,
            )
            new_target, new_over_times, new_fills = self.convert_query(query, target, legend, offset_in_sec)
            # Append the index to refId if index > 0
            if index > 0:
                new_target["refId"] = f"{start_ref_id}{index}"  # Append index to refId
            else:
                new_target["refId"] = start_ref_id  # Use the original refId for index 0
            result.append((new_target, new_over_times, new_fills))
        return result

    def convert_series_overrides(self, overrides) -> List[dict]:
        new_overrides = []
        for override in overrides:
            override = dict(override)
            # We may have converted aliases used in legend from percentile to mean. If there's visualization
            # override for the same series we need to update that as well.
            if override.get("alias"):
                override["alias"] = re.sub(r"\d+\w+\spercentile", "Mean", override["alias"])
            new_overrides.append(override)
        return new_overrides

    def get_legend_format(self, target: dict) -> str:
        legend = re.sub(r"(\[\[|\$)tag_([a-zA-Z_]+)(\]\]|)", r"{{ \2 }}", target.get("alias", ""))
        # We don't support percentile, if legend says something is percentile convert that to Mean instead
        legend = re.sub(r"\d+\w+\spercentile", "Mean", legend)
        if legend == "" and self.group_by_labels:
            legend_list = ["{{" + label + "}}" for label in self.group_by_labels]
            legend = " ".join(legend_list)

        legend = _convert_to_snake_case(legend)
        return legend

    def convert_targets(self, targets: List[dict]) -> Tuple[List[dict], List[str], List[str]]:
        new_targets = []
        r_over_times = []
        r_fills = []
        for target in targets:
            datasource = target.get("datasource")
            if datasource:
                target["datasource"] = self.update_ds_to_mimir(datasource)
                if target["datasource"].get("type") == "influxdb":
                    LOG.info(f"Skipping target as couldn't get prometheus datasource for influx {datasource}")
                    new_targets.append(target)
                    continue


            legend = self.get_legend_format(target)
            if legend:
                legend = self._replace_invalid_metric_characters(legend)  # replace to promql standard
            if target.get("rawQuery"):  # Some panels use raw query instead
                query = target["query"].replace("\n", " ")
                query = re.sub("  +", " ", query)
                new_target, over_times, fills = self.convert_query(query, target, legend, None)
                new_targets.append(new_target)
                r_fills.extend(fills)
                r_over_times.extend(over_times)
            elif target.get("measurement"):
                results = self.convert_to_query(target, legend)
                for new_target, over_times, fills in results:
                    new_targets.append(new_target)
                    r_fills.extend(fills)
                    r_over_times.extend(over_times)
            else:
                # If target does not have rawQuery or measurement selected then ignore it.
                continue
        return new_targets, r_over_times, r_fills

    def convert_panel(self, panel: dict) -> None:
        if panel["type"] == "row":
            # Collapsed panels are not listed in toplevel panels, but
            # instead under 'panels' list of the 'row'
            self.convert_panels(panel.get("panels", []))
            return

        datasource = panel["datasource"]
        if datasource.get("type") == "prometheus":
            LOG.info(f"Skipping panel as it's already prometheus datasource {datasource}")
            return

        panel["datasource"] = self.update_ds_to_mimir(datasource)
        if panel["datasource"].get("type") == "influxdb":
            LOG.info(f"Skipping panel as couldn't get prometheus datasource for influx {datasource}")
            return

        try:
            if "targets" in panel:
                targets, over_times_list, fills_list = self.convert_targets(panel["targets"])
                self.add_to_metric_and_object_list(panel['targets'], targets)
                panel["targets"] = targets
                panel["title"] = panel["title"] + " (M)"
                over_times = set(over_times_list)
                # Assume $__interval and $__rate_interval are covered by scraping interval
                for key in INTERVALS:
                    over_times.discard(key)

                if len(over_times) > 0 and not panel.get("interval", None) and "$__range" not in over_times:
                    over_time_seconds = max(_duration_to_seconds(x) for x in over_times)
                    if over_time_seconds != SCRAPE_INTERVAL_SECONDS:
                        over_time = _seconds_to_duration(over_time_seconds)
                        panel["interval"] = over_time
                fills = set(fills_list)
                fills.discard("null")  # implicit
                if "0" in fills:
                    fills.discard("0")
                    panel["nullPointMode"] = "null as zero"
                if fills:
                    LOG.warning("Unsupported fills: %r", fills)
            if "alert" in panel:
                self.convert_alerts(panel["alert"], panel['title'])
        except ValueError as ex:
            panel.pop("targets", None)
            panel.pop("alert", None)
            LOG.warning("Unable to convert: %r\nPanel: %r", ex, panel)
        if panel.get("seriesOverrides"):
            panel["seriesOverrides"] = self.convert_series_overrides(panel["seriesOverrides"])

    def convert_panels(self, panels: List[dict]) -> None:
        for panel in panels:
            self.convert_panel(panel)

    def convert_dashboard(self, dashboard: dict) -> dict:
        LOG.info(f'Started dashboard conversion for: {dashboard.get("title")}')
        # Append 'mimir' to the dashboard UID
        if "uid" in dashboard and isinstance(dashboard["uid"], str):
            dashboard["uid"] += "mimir"
            LOG.info(f'Updated dashboard UID to: {dashboard["uid"]}')

        dashboard["title"] = "(Mimir) " + dashboard["title"]
        self.current_dashboard = dashboard.get("title")
        templating = dashboard.get("templating")
        try:
            if templating:
                self.convert_templating(dashboard["templating"])
        except ValueError as ex:
            raise ConvertError(ex) from ex
        self.convert_panels(dashboard.get("panels", []))
        for row in dashboard.get("rows", []):
            # TBD does this even exist? modern Grafana seems to have just toplevel list of "panels"
            self.convert_panels(row["panels"])
        if self._get_rep_metric[0]:
            self._update_label_values_metric()

        LOG.info(f'Finished dashboard conversion for: {dashboard.get("title")}')
        return dashboard

    # Update label values query
    # If a service name is in label_values query, we want to find any metric with the same service to replace it.
    # (Unlike Influxql, Promql cannot query on services)
    def _update_label_values_metric(self):
        label_values = self._get_rep_metric[2]
        for metric in self.metric_to_objects.keys():
            splitted_metric = metric.split('_', maxsplit=2)
            label_service = self._get_rep_metric[1]
            if splitted_metric[0] == label_service or splitted_metric[0] + "_" + splitted_metric[1] == label_service:
                label_values['query'] = label_values['query'].replace(label_service, metric)
                self.metric_to_objects[metric][self.current_dashboard].append(label_values)
                self._get_rep_metric = (False, None, None)
                break
