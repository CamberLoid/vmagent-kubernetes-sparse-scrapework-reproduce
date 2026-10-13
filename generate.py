#!/usr/bin/env python3
"""Generate N Services and ServiceMonitors; write a Kubernetes List to stdout."""
import argparse
import json
import sys


def objects(count):
    for identity in range(1, count + 1):
        labels = {"reproduction-generated": "true", "reproduction-id": str(identity)}
        metadata = {"name": f"reproduction-{identity}", "namespace": "reproduction-workload", "labels": labels}
        yield {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
               "spec": {"clusterIP": "None", "selector": {"app": "reproduction-workload"},
                        "ports": [{"name": "metrics", "port": 8080, "targetPort": "metrics"}]}}
        yield {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor", "metadata": metadata,
               "spec": {"namespaceSelector": {"matchNames": ["reproduction-workload"]},
                        "selector": {"matchLabels": {"reproduction-id": str(identity)}},
                        "endpoints": [{"port": "metrics", "interval": "60s",
                                       "params": {"id": [str(identity)]}}]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.count:
        parser.error("count must be > 1")
    json.dump({"apiVersion": "v1", "kind": "List", "items": list(objects(args.count))}, sys.stdout)
    print()


if __name__ == "__main__":
    main()
