# Reproduction of https://github.com/VictoriaMetrics/VictoriaMetrics/pull/11751

[English](README.md)

该仓库使用真实 Kubernetes 对象和 vmagent 原生分片，复现 [Patch](https://github.com/VictoriaMetrics/VictoriaMetrics/pull/11751) 所解决的问题，并输出对比验证。

## 架构与隔离

| Namespace | 资源 | 监控配置的选择范围 |
|---|---|---|
| `reproduction-stack` | Operator、Grafana、VMSingle、常规监控 vmagent、集群采集器 | 常规 vmagent 仅选择此处的 ServiceScrape／NodeScrape |
| `reproduction-monitor` | 被测 VMAgent、自监控 metrics Service | 被测实例选择 workload 中生成的 ServiceScrape |
| `reproduction-workload` | 一个 metrics Pod、N 个 headless Service 和 ServiceMonitor | 每个 Monitor 选择一个 Service，并传入 ID |

Operator 将测试 ServiceMonitor 转为 VMServiceScrape。stack 自带 vmagent 排除这些配置；被测进程的自监控配置位于 stack namespace，因此仍被采集。

各 job 共享发现对象，再经 relabel 和分片筛选；明确关闭 Kubernetes API selectors。每 target 返回 1000 条样本，固定标签，返回随机数。

测试使用 **10000 个对象、2 shards × 1 replica**，被测 vmagent 每 Pod limit 为 **6 CPU / 24Gi**，VMSingle limit 为 **4 CPU / 8Gi**；

## 文件

- `namespace.yaml`：三个 Namespace。
- `reproduction.yaml`：workload、自监控和被测 VMAgent（2 shards × 1 replica）。
- `values-vm-k8s-stack.yaml`：Helm stack 与集群监控配置。
- `reproduction.py`：metrics 服务脚本，用 `kubectl create cm` 提供。
- `generate.py`：生成 N 组 Service／ServiceMonitor。

Helm 渲染资源和批量生成的对象独立于静态 manifest，避免复制 chart 或手写成千上万个重复资源。

## 1. 环境准备

需要的

- Kubernetes (Uses K3s as example)
- Helm 4

命令从仓库根目录执行；

```sh
# 准备 K3s
curl -fsSL 'https://raw.githubusercontent.com/k3s-io/k3s/v1.37.1+k3s1/install.sh' -o /tmp/install-k3s.sh
sudo env INSTALL_K3S_VERSION='v1.37.1+k3s1' sh /tmp/install-k3s.sh server --disable traefik --disable servicelb
mkdir -p "$HOME/.kube"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" /etc/rancher/k3s/k3s.yaml "$HOME/.kube/reproduction.yaml"
export KUBECONFIG="$HOME/.kube/reproduction.yaml"
kubectl wait --for=condition=Ready nodes --all --timeout=300s

# 准备 Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash
```

## 2. 安装 CRDs 和 stack

```sh
mkdir -p generated results
# 仅安装 Prometheus Operator CRD
curl -fsSL https://github.com/prometheus-operator/prometheus-operator/releases/download/v0.94.1/stripped-down-crds.yaml -o generated/prometheus-crds.yaml
kubectl apply --server-side -f generated/prometheus-crds.yaml
kubectl wait --for=condition=Established crd/servicemonitors.monitoring.coreos.com --timeout=120s
# 安装 victoria-metrics-k8s-stack
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update vm
helm upgrade --install reproduction-stack vm/victoria-metrics-k8s-stack --version 0.96.0 -n reproduction-stack --create-namespace -f values-vm-k8s-stack.yaml --wait --timeout=10m
kubectl wait --for=condition=Established crd/vmagents.operator.victoriametrics.com --timeout=120s
kubectl wait --for=condition=Established crd/vmservicescrapes.operator.victoriametrics.com --timeout=120s
```

## 3. 部署 workload

先 apply manifest 创建 namespace 再创建脚本 ConfigMap 最后是实例。等待 workload 就绪后生成监控对象：

```sh
kubectl apply -f namespace.yaml
kubectl create cm reproduction -n reproduction-workload --from-file=reproduction.py
kubectl apply -f reproduction.yaml
kubectl rollout status deployment/reproduction-workload -n reproduction-workload --timeout=300s
python3 generate.py --count 10000 > generated/workload.json
kubectl apply -f generated/workload.json
# kubectl get services,servicemonitors,vmservicescrapes,endpoints -n reproduction-workload -l reproduction-generated=true
```

等待 10000 个 Service、ServiceMonitor、转换后的 VMServiceScrape 和 Endpoints 就绪，每个 Endpoint 有一个 ready address。转换与对象更新异步。完整对象集合准备好后，按下方命令重建被测 Pod，确保冷启动走全量重建路径。

## 4. 运行基线并采证

```sh
# 强制重启 VMAgent
kubectl delete pods -n reproduction-monitor -l reproduction-subject=true
kubectl get pods -n reproduction-monitor -l reproduction-subject=true -o wide
```

等待两个被测 Pod Ready，选择一个，从 Pod 参数记录 `promscrape.cluster.memberNum`。在 K3s 节点或能路由到 Pod 网段的机器上，直接访问 Pod IP：

```sh
POD='<subject-pod>'
POD_IP=$(kubectl get pod "$POD" -n reproduction-monitor -o jsonpath='{.status.podIP}')
VMAGENT_URL="http://${POD_IP}:8429"
```

确认 HTTP `/ready` 返回 200 后，观察十分钟再采集：

```sh
RUN=v1.154.0
curl -fsS "$VMAGENT_URL/config" > "results/$RUN-config.yaml"
curl -fsS "$VMAGENT_URL/metrics" > "results/$RUN-metrics.txt"
curl -fsS "$VMAGENT_URL/api/v1/targets?state=active" > "results/$RUN-targets.json"
curl -fsS "$VMAGENT_URL/debug/pprof/heap?gc=1" > "results/$RUN-heap.pb.gz"
```

## 5. 运行修复版本

```sh
PATCHED_REPOSITORY='ghcr.io/camberloid/vmagent'
PATCHED_TAG='v1.154.0-k8s-sparse-0c77a9a35-test'
kubectl patch vmagent reproduction -n reproduction-monitor --type=merge \
  -p "{\"spec\":{\"image\":{\"repository\":\"$PATCHED_REPOSITORY\",\"tag\":\"$PATCHED_TAG\"}}}"
kubectl get pods -n reproduction-monitor -l reproduction-subject=true -o wide
```

确认所有被测 Pod 已使用补丁镜像并 Ready，选择同一 shard／replica 的新 Pod。先更新 Pod IP，再采集 `v1.154.0-fix` 数据。运行期间不要重新 apply `reproduction.yaml`，否则会恢复初始基线配置。

重新收集：

```sh
POD='<patched-pod-for-the-same-shard>'
POD_IP=$(kubectl get pod "$POD" -n reproduction-monitor -o jsonpath='{.status.podIP}')
VMAGENT_URL="http://${POD_IP}:8429"
RUN=v1.154.0-fix
curl -fsS "$VMAGENT_URL/config" > "results/$RUN-config.yaml"
curl -fsS "$VMAGENT_URL/metrics" > "results/$RUN-metrics.txt"
curl -fsS "$VMAGENT_URL/api/v1/targets?state=active" > "results/$RUN-targets.json"
curl -fsS "$VMAGENT_URL/debug/pprof/heap?gc=1" > "results/$RUN-heap.pb.gz"
```

```sh
diff -u results/v1.154.0-config.yaml results/v1.154.0-fix-config.yaml
go tool pprof -top -sample_index=inuse_objects results/v1.154.0-heap.pb.gz >> results/v1.154.0-heap.inuse_objects.txt
go tool pprof -top -sample_index=inuse_objects results/v1.154.0-fix-heap.pb.gz >> results/v1.154.0-fix-heap.inuse_objects.txt
go tool pprof -top -sample_index=inuse_space results/v1.154.0-heap.pb.gz >> results/v1.154.0-heap.inuse_space.txt
go tool pprof -top -sample_index=inuse_space results/v1.154.0-fix-heap.pb.gz >> results/v1.154.0-fix-heap.inuse_space.txt
go tool pprof -top -sample_index=alloc_space results/v1.154.0-heap.pb.gz >> results/v1.154.0-heap.alloc_space.txt
go tool pprof -top -sample_index=alloc_space results/v1.154.0-fix-heap.pb.gz >> results/v1.154.0-fix-heap.alloc_space.txt
```

`inuse_objects` 是存活对象数量，`inuse_space` 是采样的存活 heap 字节数，`alloc_space` 是累计分配字节数。`>>` 追加报告；重复执行前清空对应 txt，避免混入多次输出。下面的结论使用取回的 profile 和 metrics，不把对象数量、累计分配或 RSS 混为同一指标。

## Grafana

已启用 Grafana 并通过 NodePort 30080 暴露

## 集群监控

已启用 kubelet/cAdvisor、kube-state-metrics 和 node-exporter。在 Grafana **Explore → VictoriaMetrics** 查询：

| 视图 | PromQL |
|---|---|
| 就绪的被测 Pod 数 | `sum(kube_pod_status_ready{namespace="reproduction-monitor",condition="true"})` |
| 被测 Pod 的 CPU 总量（核） | `sum(rate(container_cpu_usage_seconds_total{namespace="reproduction-monitor",container!="",container!="POD"}[5m]))` |
| 被测 Pod 的 working set 总量（GiB） | `sum(container_memory_working_set_bytes{namespace="reproduction-monitor",container!="",container!="POD"}) / 1024^3` |
| 被测 Pod 的 memory requests 总量（GiB） | `sum(kube_pod_container_resource_requests{namespace="reproduction-monitor",resource="memory",unit="byte"}) / 1024^3` |
| 被测 Pod 的 memory limits 总量（GiB） | `sum(kube_pod_container_resource_limits{namespace="reproduction-monitor",resource="memory",unit="byte"}) / 1024^3` |
| 被测 Pod 最近 15 分钟重启次数 | `sum(increase(kube_pod_container_status_restarts_total{namespace="reproduction-monitor"}[15m]))` |
| 最近终止原因为 OOM 的被测容器数 | `sum(kube_pod_container_status_last_terminated_reason{namespace="reproduction-monitor",reason="OOMKilled"} == 1)` |
| 集群节点可用内存总量（GiB） | `sum(node_memory_MemAvailable_bytes) / 1024^3` |

聚合包含所有 shard、副本和 sidecar；使用 `sum by (pod)(...)` 拆到单 Pod。OOM 指标表示最近终止状态，不是事件次数（[指标定义](https://github.com/kubernetes/kube-state-metrics/blob/v2.20.0/docs/metrics/workload/pod-metrics.md)）。Kubernetes Ready 不替代 vmagent `/ready`。查不到数据时检查常规监控 vmagent 的 `/targets`，不能把空结果当成零用量。两版本对比时保持采集器开启。

## 清理

先保存所需结果。

如果是全新部署 Kubernetes：直接清理集群即可

```sh
# 适用于本文新建的单节点 K3s server，会删除本机集群数据和本地 PV 数据
sudo /usr/local/bin/k3s-uninstall.sh
```

否则可以参考以下步骤：

Operator 仍运行时删除 VM 资源，再卸载 stack 和删除静态 manifest：

```sh
kubectl delete vmagent reproduction -n reproduction-monitor --ignore-not-found
kubectl delete vmsingle reproduction-stack -n reproduction-stack --ignore-not-found --timeout=600s
helm uninstall reproduction-stack -n reproduction-stack
kubectl delete -f reproduction.yaml --ignore-not-found
# 未清理 CRDs
```

## Results

`./results` 保留了最近一次维护者在自己环境运行的复现结果；通常情况下为 ignored。

可以使用 `git add -f results` 添加最新的验证结果。

### 2026-10-13 

说明：

- 数据中的运行时可用 CPU 为每进程 4，`process_memory_limit_bytes` 为 30 GiB
- 进程采样时分别运行约 10 分 13 秒和 7 分 4 秒

#### 测量结果

以下均为**所采集的单个进程**；GiB / MiB 按 1024 进制换算。metrics 快照与随后强制 GC 的 profile 是不同测量点。

| 指标 | v1.154.0 | v1.154.0-fix | 下降 |
|---|---:|---:|---:|
| RSS（metrics 快照） | 12.703 GiB | 0.728 GiB | 94.27% |
| Go heap alloc（metrics 快照） | 10.271 GiB | 0.427 GiB | 95.85% |
| 峰值 RSS（进程生命周期内） | 12.704 GiB | 1.076 GiB | 91.53% |
| GC 后采样存活 heap（`inuse_space`） | 7.776 GiB | 0.405 GiB | 94.79% |
| 构建闭包直接归因的存活分配（flat） | 7539.84 MiB | 0.50 MiB | 99.99% |
| 采样存活对象总数（`inuse_objects`） | 3,250,278 | 3,235,121 | — |

基线 profile 中，`getScrapeWorkObjectsByAPIWatcherLocked.func1` 占采样存活 heap 的 **94.69%**；修复后为 **0.12%**。对象总数接近，但存活字节数显著下降，与消除空结果 map 底层容量保留的机制一致。

来源：[基线 metrics](results/v1.154.0-metrics.txt)、[修复版 metrics](results/v1.154.0-fix-metrics.txt)、[基线 heap](results/v1.154.0-heap.pb.gz)、[修复版 heap](results/v1.154.0-fix-heap.pb.gz)。目标比较只使用标签和 `scrapeUrl`，排除抓取时间、耗时和动态样本值。

#### 图片与结论

本次记录支持：补丁在保持配置及所采集 shard 的目标集合一致时，大幅降低 Kubernetes 全量重建结果所保留的内存。

**基线：CPU / RSS 与目标状态。**

![基线 CPU 与 RSS](results/v1.154.0-grafana-screenshot-1.png)

![基线目标状态](results/v1.154.0-grafana-screenshot-2.png)

**修复版：CPU / RSS 与目标状态。** RSS 截图保留了切换前的历史实例曲线；修复后的新实例曲线低于 1 GB，具体数值以上方单进程 metrics 为准。两组状态截图均显示 10000 个目标 up、0 个 down，这是 dashboard 的聚合视图。

![修复版 CPU 与 RSS](results/v1.154.0-fix-grafana-screenshot-2.png)

![修复版目标状态](results/v1.154.0-fix-grafana-screenshot-1.png)
