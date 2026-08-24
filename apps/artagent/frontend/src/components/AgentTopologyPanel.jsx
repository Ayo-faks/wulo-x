import React, { useMemo, useState } from 'react';

const cardStyle = {
  border: "1px solid var(--vsc-border)",
  borderRadius: 10,
  padding: 12,
  background: "linear-gradient(135deg, var(--vsc-panel-bg) 0%, var(--vsc-panel-bg) 100%)",
  boxShadow: "0 4px 10px rgba(0,0,0,0.04)",
};

const connectionStyle = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 10px",
  backgroundColor: "var(--vsc-panel-bg)",
  border: "1px dashed rgba(var(--vsc-accent-rgb), 0.24)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--vsc-accent)",
};

const AgentTopologyPanel = ({ inventory, activeAgent, onClose }) => {
  const [expandedAgent, setExpandedAgent] = useState(null);

  const {
    agents = [],
    startAgent = null,
    scenario = null,
    handoffMap = {},
  } = inventory || {};

  const connections = useMemo(
    () => Object.entries(handoffMap || {}).map(([tool, target]) => ({ tool, target })),
    [handoffMap],
  );

  const previewNames = useMemo(
    () => agents.slice(0, 3).map((a) => a.name).join(", "),
    [agents],
  );

  const selected = useMemo(() => {
    if (!agents.length) return null;
    const found = agents.find((a) => a.name === expandedAgent);
    return found || agents[0];
  }, [agents, expandedAgent]);

  if (!agents.length) {
    return null;
  }

  const toolList = useMemo(() => {
    if (!selected) return [];
    return Array.from(
      new Set(
        (selected.tools_preview ||
          selected.tools ||
          selected.tool_names ||
          selected.toolNames ||
          []).filter(Boolean),
      ),
    );
  }, [selected]);

  return (
    <div
      className="agents-panel"
      style={{
        position: "fixed",
        left: 12,
        bottom: 120,
        width: 520,
        maxHeight: "70vh",
        overflow: "auto",
        zIndex: 30,
        background: "rgba(255,255,255,0.98)",
        borderRadius: 14,
        boxShadow: "0 18px 50px rgba(15,23,42,0.22)",
        border: "1px solid var(--vsc-border)",
        padding: 12,
        scrollbarWidth: "none",
        msOverflowStyle: "none",
      }}
    >
      <div
        style={{
          position: "sticky",
          top: 0,
          background: "rgba(255,255,255,0.98)",
          paddingBottom: 8,
          marginBottom: 8,
          zIndex: 1,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ fontWeight: 700, color: "var(--vsc-fg)", letterSpacing: 0.2 }}>Agents</div>
            <div style={{ fontSize: 11, color: "var(--vsc-fg-muted)", display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ padding: "2px 6px", borderRadius: 999, background: "var(--vsc-panel-bg)", color: "var(--vsc-accent)", border: "1px solid rgba(var(--vsc-accent-rgb), 0.16)" }}>
                {agents.length} agents
              </span>
              {scenario && (
                <span style={{ padding: "2px 6px", borderRadius: 999, background: "rgba(var(--vsc-accent-rgb), 0.12)", color: "var(--vsc-link)", border: "1px solid rgba(var(--vsc-accent-rgb), 0.18)" }}>
                  scenario: {scenario}
                </span>
              )}
              {activeAgent && (
                <span style={{ padding: "2px 6px", borderRadius: 999, background: "rgba(78, 201, 176, 0.18)", color: "var(--vsc-success)", border: "1px solid rgba(78, 201, 176, 0.20)" }}>
                  active: {activeAgent}
                </span>
              )}
            </div>
          </div>
          {typeof onClose === "function" && (
            <button
              type="button"
              onClick={onClose}
              style={{
                border: "1px solid var(--vsc-border)",
                borderRadius: 8,
                background: "var(--vsc-panel-bg)",
                padding: "4px 8px",
                fontSize: 12,
                color: "var(--vsc-fg-muted)",
                cursor: "pointer",
              }}
            >
              Close
            </button>
          )}
        </div>
      </div>

      <div
        style={{
          fontSize: 12,
          color: "var(--vsc-fg-muted)",
          padding: "10px 12px",
          border: "1px dashed var(--vsc-border)",
          borderRadius: 10,
          background: "var(--vsc-panel-bg)",
          marginBottom: 12,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Preview</div>
        <div>{previewNames || "Agents loaded"}</div>
      </div>

      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
        {agents.map((agent, idx) => {
          const isSelected = selected?.name === agent.name;
          return (
            <div
              key={idx}
              style={{
                ...cardStyle,
                borderColor: isSelected ? "var(--vsc-link)" : cardStyle.border,
                boxShadow: isSelected ? "0 8px 24px rgba(59,130,246,0.18)" : cardStyle.boxShadow,
                cursor: "pointer",
              }}
              onClick={() => setSelectedAgent(agent.name)}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div style={{ fontWeight: 700, color: "var(--vsc-fg)" }}>{agent.name}</div>
                {startAgent === agent.name && (
                  <span style={{ fontSize: 11, padding: "2px 6px", borderRadius: 999, background: "rgba(78, 201, 176, 0.18)", color: "var(--vsc-success)", border: "1px solid var(--vsc-success)" }}>
                    start
                  </span>
                )}
              </div>
              {agent.description && (
                <div style={{ marginTop: 6, color: "var(--vsc-fg-muted)", fontSize: 12, lineHeight: 1.4 }}>
                  {agent.description}
                </div>
              )}
              <div style={{ marginTop: 8, display: "flex", gap: 8, flexWrap: "wrap", fontSize: 11, color: "var(--vsc-fg)" }}>
                {agent.model && (
                  <span style={{ padding: "2px 6px", background: "var(--vsc-panel-bg)", borderRadius: 8 }}>
                    Model: {typeof agent.model === "string" ? agent.model.replace(/^gpt-/, "") : agent.model}
                  </span>
                )}
                {agent.voice && (
                  <span style={{ padding: "2px 6px", background: "rgba(204, 167, 0, 0.18)", borderRadius: 8 }}>
                    Voice: {typeof agent.voice === "string" ? (agent.voice.split("-").pop() || agent.voice) : agent.voice}
                  </span>
                )}
                {typeof agent.toolCount === "number" && (
                  <span style={{ padding: "2px 6px", background: "rgba(var(--vsc-accent-rgb), 0.12)", borderRadius: 8 }}>Tools: {agent.toolCount}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {selected && (
        <div
          style={{
            marginTop: 14,
            padding: "12px",
            borderRadius: 10,
            border: "1px solid var(--vsc-border)",
            background: "var(--vsc-panel-bg)",
            boxShadow: "0 6px 14px rgba(15,23,42,0.08)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <div style={{ fontWeight: 700, color: "var(--vsc-fg)" }}>{selected.name}</div>
            <div style={{ display: "flex", gap: 6, fontSize: 11, color: "var(--vsc-fg-muted)" }}>
              {selected.model && <span style={{ padding: "2px 6px", borderRadius: 8, background: "var(--vsc-input-bg)" }}>Model: {selected.model}</span>}
              {selected.voice && <span style={{ padding: "2px 6px", borderRadius: 8, background: "rgba(204, 167, 0, 0.18)" }}>Voice: {selected.voice}</span>}
              {selected.handoff_trigger && <span style={{ padding: "2px 6px", borderRadius: 8, background: "rgba(var(--vsc-accent-rgb), 0.12)" }}>Handoff: {selected.handoff_trigger}</span>}
            </div>
          </div>
          {selected.description && (
            <div style={{ fontSize: 12, color: "var(--vsc-fg-muted)", marginBottom: 8 }}>
              {selected.description}
            </div>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {toolList.length > 0 ? (
              toolList.map((tool, idx) => (
                <span key={idx} style={{ padding: "4px 8px", borderRadius: 8, background: "var(--vsc-panel-bg)", color: "var(--vsc-accent)", fontSize: 12, border: "1px solid rgba(var(--vsc-accent-rgb), 0.16)" }}>
                  {tool}
                </span>
              ))
            ) : selected.toolCount > 0 ? (
              <span style={{ fontSize: 12, color: "var(--vsc-fg-muted)" }}>
                {selected.toolCount} tools declared (names unavailable)
              </span>
            ) : (
              <span style={{ fontSize: 12, color: "var(--vsc-fg-muted)" }}>No tools declared</span>
            )}
          </div>
        </div>
      )}

      {connections.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontWeight: 600, color: "var(--vsc-fg)", marginBottom: 6 }}>Handoff routes</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {connections.map(({ tool, target }) => (
              <div key={`${tool}-${target}`} style={connectionStyle}>
                <span style={{ fontWeight: 700 }}>{tool}</span>
                <span style={{ color: "var(--vsc-fg-muted)" }}>-&gt;</span>
                <span style={{ fontWeight: 600 }}>{target}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default AgentTopologyPanel;
