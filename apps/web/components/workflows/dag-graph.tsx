"use client";

import {
  Background,
  Controls,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Bot, GitMerge, ShieldCheck, Sparkles, Wrench } from "lucide-react";
import { memo, useMemo, useSyncExternalStore } from "react";

import { StatusBadge } from "@/components/ui/status";
import { cn } from "@/lib/utils";

export type DagNodeSpec = {
  key: string;
  type: string;
  name?: string | null;
  agent?: string | null;
  tool?: string | null;
  depends_on?: string[] | null;
};

export type DagNodeState = { status: string; attempts?: number; failures?: number; agentName?: string | null };

type NodeData = DagNodeSpec & { state?: DagNodeState; selected: boolean };

const TYPE_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  agent_task: Bot,
  tool_task: Wrench,
  approval_gate: ShieldCheck,
  aggregation: GitMerge,
  plan: Sparkles,
};

const BORDER: Record<string, string> = {
  running: "border-info",
  succeeded: "border-success",
  failed: "border-danger",
  waiting_approval: "border-warning",
  retry_wait: "border-warning",
  ready: "border-primary/60",
  cancelled: "border-border opacity-70",
  skipped: "border-border opacity-60",
};

const DagNode = memo(function DagNode({ data }: NodeProps<Node<NodeData>>) {
  const Icon = TYPE_ICON[data.type] ?? Wrench;
  const status = data.state?.status;
  return (
    <div
      className={cn(
        "w-[220px] rounded-lg border-2 bg-card p-3 text-left shadow-sm transition-colors",
        status ? BORDER[status] ?? "border-border" : "border-border",
        status === "running" && "node-running",
        data.selected && "ring-2 ring-primary ring-offset-2 ring-offset-background",
      )}
      data-testid={`dag-node-${data.key}`}
      data-status={status ?? "definition"}
    >
      <Handle type="target" position={Position.Left} className="!bg-muted-foreground" />
      <div className="flex items-center gap-2">
        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate text-sm font-medium" title={data.name ?? data.key}>
          {data.name ?? data.key}
        </span>
      </div>
      <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{data.key}</div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">
        {data.type.replace("_", " ")}
        {data.state?.agentName || data.agent ? ` · ${data.state?.agentName ?? data.agent}` : ""}
        {data.tool ? ` · ${data.tool}` : ""}
      </div>
      {status ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <StatusBadge status={status} />
          {data.state?.attempts ? (
            <span className="text-[11px] text-muted-foreground" title="attempts">
              ×{data.state.attempts}
              {data.state.failures ? <span className="text-danger"> ({data.state.failures} failed)</span> : null}
            </span>
          ) : null}
        </div>
      ) : null}
      <Handle type="source" position={Position.Right} className="!bg-muted-foreground" />
    </div>
  );
});

const nodeTypes = { dag: DagNode };

export function computeLevels(nodes: DagNodeSpec[]): Record<string, number> {
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const levels: Record<string, number> = {};
  const visit = (key: string, stack: Set<string>): number => {
    if (levels[key] !== undefined) return levels[key];
    if (stack.has(key)) return 0; // cycle: validation reports it; keep layout finite
    stack.add(key);
    const deps = (byKey.get(key)?.depends_on ?? []).filter((d) => byKey.has(d));
    const lvl = deps.length ? Math.max(...deps.map((d) => visit(d, stack))) + 1 : 0;
    stack.delete(key);
    levels[key] = lvl;
    return lvl;
  };
  nodes.forEach((n) => visit(n.key, new Set()));
  return levels;
}

function subscribeHtmlClass(cb: () => void) {
  const obs = new MutationObserver(cb);
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => obs.disconnect();
}

export function DagGraph({
  nodes,
  states,
  selectedKey,
  onSelect,
  height = 460,
}: {
  nodes: DagNodeSpec[];
  states?: Record<string, DagNodeState>;
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
  height?: number;
}) {
  const dark = useSyncExternalStore(subscribeHtmlClass, () => document.documentElement.classList.contains("dark"), () => true);
  // Remount (and re-fit the viewport) when the graph's shape changes, e.g. after AI planning.
  const shapeKey = nodes.map((n) => `${n.key}:${(n.depends_on ?? []).join(",")}`).join("|");
  const { flowNodes, flowEdges } = useMemo(() => {
    const levels = computeLevels(nodes);
    const columns = new Map<number, DagNodeSpec[]>();
    for (const n of nodes) {
      const l = levels[n.key] ?? 0;
      columns.set(l, [...(columns.get(l) ?? []), n]);
    }
    const maxRows = Math.max(1, ...[...columns.values()].map((c) => c.length));
    const flowNodes: Node<NodeData>[] = [];
    for (const [level, col] of columns) {
      const offset = ((maxRows - col.length) * 120) / 2;
      col.forEach((n, i) =>
        flowNodes.push({
          id: n.key,
          type: "dag",
          position: { x: level * 290, y: offset + i * 120 },
          data: { ...n, state: states?.[n.key], selected: n.key === selectedKey },
          draggable: false,
        }),
      );
    }
    const keys = new Set(nodes.map((n) => n.key));
    const flowEdges: Edge[] = nodes.flatMap((n) =>
      (n.depends_on ?? [])
        .filter((d) => keys.has(d))
        .map((d) => {
          const src = states?.[d]?.status;
          const dst = states?.[n.key]?.status;
          const color = src === "succeeded" ? "var(--success)" : src === "failed" ? "var(--danger)" : "var(--muted-foreground)";
          return {
            id: `${d}->${n.key}`,
            source: d,
            target: n.key,
            animated: dst === "running" || dst === "ready",
            style: { stroke: color, strokeWidth: 1.5 },
            markerEnd: { type: MarkerType.ArrowClosed, color },
          };
        }),
    );
    return { flowNodes, flowEdges };
  }, [nodes, states, selectedKey]);

  return (
    <div className="rounded-lg border bg-background" style={{ height }} data-testid="dag-graph">
      <ReactFlow
        key={shapeKey}
        colorMode={dark ? "dark" : "light"}
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        nodesConnectable={false}
        nodesFocusable
        onNodeClick={(_, node) => onSelect?.(node.id)}
        onKeyDown={(e) => {
          const target = e.target as HTMLElement;
          const id = target.closest("[data-id]")?.getAttribute("data-id");
          if (id && (e.key === "Enter" || e.key === " ")) onSelect?.(id);
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
