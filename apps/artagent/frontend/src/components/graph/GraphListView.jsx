import React, { useMemo, useState, useEffect } from 'react';
import { Box } from '@mui/material';
import PersonRoundedIcon from '@mui/icons-material/PersonRounded';
import SmartToyRoundedIcon from '@mui/icons-material/SmartToyRounded';
import SettingsEthernetRoundedIcon from '@mui/icons-material/SettingsEthernetRounded';
import { styles } from '../../styles/voiceAppStyles.js';
import { formatStatusTimestamp } from '../../utils/formatters.js';

const GraphListView = ({ events, compact = true, fillHeight = false }) => {
  const [selectedFilters, setSelectedFilters] = useState([]);
  const recentEvents = useMemo(() => {
    return events
      .filter((evt) => {
        const from = evt.from || evt.agent;
        const to = evt.to || evt.agent;
        if (!from || !to) return false;
        const bothSystem = from === "System" && to === "System";
        return !bothSystem;
      })
      .slice(-60);
  }, [events]);
  const agentList = useMemo(() => {
    const names = new Set();
    recentEvents.forEach((evt) => {
      if (evt.from) names.add(evt.from);
      if (evt.to) names.add(evt.to);
      if (evt.agent) names.add(evt.agent);
    });
    return Array.from(names);
  }, [recentEvents]);
  const paletteByName = useMemo(() => {
    const map = new Map();
    const colors = [
      "var(--vsc-link)",
      "rgba(78, 201, 176, 0.24)",
      "var(--vsc-warning)",
      "rgba(241, 76, 76, 0.24)",
      "var(--vsc-link)",
      "rgba(var(--vsc-accent-rgb), 0.24)",
      "var(--vsc-warning)",
      "var(--vsc-link)",
      "rgba(var(--vsc-accent-rgb), 0.20)",
      "rgba(var(--vsc-accent-rgb), 0.24)",
    ];
    agentList.forEach((name, idx) => {
      map.set(name, colors[idx % colors.length]);
    });
    return map;
  }, [agentList]);

  const filteredEvents = useMemo(() => {
    if (!selectedFilters.length) return recentEvents;
    return recentEvents.filter((evt) => {
      const participants = [
        evt.from || evt.agent,
        evt.to || evt.agent,
        evt.agent,
      ].filter(Boolean);
      return participants.some((p) => selectedFilters.includes(p));
    });
  }, [recentEvents, selectedFilters]);

  // Leave filters empty by default (show all) and let the user pick any agent
  useEffect(() => {
    if (!recentEvents.length) return;
    // no-op; keep "All" as default
  }, [recentEvents]);

  const toggleFilter = (name) => {
    setSelectedFilters((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]
    );
  };

  if (!recentEvents.length) {
    return null;
  }

  const kindLabel = (kind) =>
    ({
      message: "Message",
      tool: "Tool",
      switch: "Switch",
      event: "Event",
      function: "Function",
    }[kind] || "Message");

  const containerStyle = compact
    ? styles.graphContainer
    : { ...styles.graphContainer, maxWidth: "95%", overflowX: "hidden" };

  if (fillHeight) {
    containerStyle.height = "100%";
    containerStyle.minHeight = "100%";
    containerStyle.display = "flex";
    containerStyle.flexDirection = "column";
  }

  return (
    <Box style={containerStyle}>
      <div style={styles.graphHeader}>
        <div>
          <div style={styles.graphTitle}>Agent Flow</div>
          <div style={styles.graphSubtitle}>
            Recent agent messages, tool calls, and handoffs
          </div>
        </div>
        <div style={styles.graphSubtitle}>
          Showing last {filteredEvents.length} events
        </div>
      </div>

      <div style={{ ...styles.graphAgentsRow, flexWrap: "wrap" }}>
        <span
          key="all"
          style={{
            ...styles.graphAgentChip,
            background: !selectedFilters.length ? "rgba(59,130,246,0.15)" : "rgba(226,232,240,0.7)",
            borderColor: !selectedFilters.length ? "rgba(59,130,246,0.4)" : "rgba(148,163,184,0.35)",
          }}
          onClick={() => setSelectedFilters([])}
        >
          All
        </span>
        {agentList.map((agent) => {
          const color = paletteByName.get(agent) || "var(--vsc-fg)";
          const active = selectedFilters.includes(agent);
          return (
            <span
              key={agent}
              style={{
                ...styles.graphAgentChip,
                background: active ? `${color}33` : "rgba(226,232,240,0.7)",
                borderColor: active ? color : "rgba(148,163,184,0.35)",
                color: active ? "var(--vsc-fg)" : "var(--vsc-fg)",
                boxShadow: active ? `0 4px 10px ${color}44` : "none",
                cursor: "pointer",
              }}
              onClick={() => toggleFilter(agent)}
            >
              {agent}
            </span>
          );
        })}
      </div>

      <div
        style={{
          ...styles.graphEventsList,
          flex: fillHeight ? 1 : undefined,
          minHeight: fillHeight ? 0 : undefined,
        }}
      >
        {filteredEvents.map((evt) => {
          const ts = formatStatusTimestamp(evt.ts);
          const from = evt.from || evt.agent || "System";
          const to = evt.to || evt.agent || "User";
          const text = evt.text || evt.detail || evt.tool || "";
          const isLong = text && text.length > 140;
          const preview = isLong ? `${text.slice(0, 140)}…` : text;
          const fromColor = paletteByName.get(from) || "var(--vsc-fg)";
          const toColor = paletteByName.get(to) || "var(--vsc-fg)";
          const iconFor = (name) => {
            if (name === "User") return <PersonRoundedIcon sx={{ fontSize: 14, color: toColor }} />;
            if (name === "System") return <SettingsEthernetRoundedIcon sx={{ fontSize: 14, color: toColor }} />;
            return <SmartToyRoundedIcon sx={{ fontSize: 14, color: toColor }} />;
          };
          return (
            <details key={evt.id} style={{ ...styles.graphEventRow, padding: "10px 12px" }}>
              <summary style={{ display: "flex", alignItems: "center", gap: "12px", cursor: "pointer", listStyle: "none", outline: "none", width: "100%", boxSizing: "border-box", minWidth: 0, flexWrap: "wrap" }}>
                <div style={styles.graphEventMeta}>
                  <span style={styles.graphBadge(evt.kind)}>{kindLabel(evt.kind)}</span>
                  {ts && <span style={styles.graphTimestamp}>{ts}</span>}
                </div>
                <div style={{ ...styles.graphFlow, flex: 1, minWidth: 0, flexWrap: "wrap" }}>
                  <span style={{ ...styles.graphNode(), background: `${fromColor}22`, borderColor: fromColor, color: "var(--vsc-fg)", display: "inline-flex", alignItems: "center", gap: "6px" }}>
                    {iconFor(from)}
                    {from}
                  </span>
                  <span style={{ color: "var(--vsc-fg-muted)", fontSize: "12px" }}>→</span>
                  <span style={{ ...styles.graphNode("target"), background: `${toColor}22`, borderColor: toColor, color: "var(--vsc-fg)", display: "inline-flex", alignItems: "center", gap: "6px" }}>
                    {iconFor(to)}
                    {to}
                  </span>
                  {(preview || evt.tool) && (
                    <span style={{ ...styles.graphText, marginLeft: "10px", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "normal", wordBreak: "break-word" }}>
                      {evt.tool ? `Tool: ${evt.tool}` : null}
                      {evt.tool && preview ? " • " : ""}
                      {preview}
                    </span>
                  )}
                </div>
              </summary>
              {isLong && (
                <div style={{ ...styles.graphText, marginTop: "8px", wordBreak: "break-word" }}>
                  {text}
                </div>
              )}
              {evt.data && typeof evt.data === "object" && Object.keys(evt.data).length > 0 && (
                <div style={{ marginTop: "8px", fontSize: "11px", color: "var(--vsc-fg-muted)", overflowX: "auto" }}>
                  <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {JSON.stringify(evt.data, null, 2)}
                  </pre>
                </div>
              )}
            </details>
          );
        })}
      </div>
    </Box>
  );
};

export default GraphListView;
