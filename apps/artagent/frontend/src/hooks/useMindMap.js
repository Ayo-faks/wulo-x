// src/hooks/useMindMap.js
import { useState, useRef } from "react";

export default function useMindMap() {
  const rootUser = {
    id: "user-root",
    data: { label: "👤 User" },
    position: { x: -220, y: 0 },
    style: { background: "var(--vsc-success)", color: "var(--vsc-fg-strong)" },
  };
  const rootAssistant = {
    id: "assistant-root",
    data: { label: "🤖 Assistant" },
    position: { x: 220, y: 0 },
    style: { background: "var(--vsc-accent)", color: "var(--vsc-fg-strong)" },
  };

  const [nodes, setNodes] = useState([rootUser, rootAssistant]);
  const [edges, setEdges] = useState([
    {
      id: "e-user-to-assistant",
      source: "user-root",
      target: "assistant-root",
      animated: false,
      style: { stroke: "var(--vsc-fg-muted)" },
    },
    {
      id: "e-assistant-to-user",
      source: "assistant-root",
      target: "user-root",
      animated: false,
      style: { stroke: "var(--vsc-fg-muted)" },
    },
  ]);

  const idRef = useRef(0);
  const nextId = () => `n-${Date.now()}-${idRef.current++}`;

  const lastUserId = useRef("user-root");
  const lastAssistantId = useRef("assistant-root");

  const resetMindMap = () => {
    setNodes([rootUser, rootAssistant]);
    setEdges([
      {
        id: "e-user-to-assistant",
        source: "user-root",
        target: "assistant-root",
        animated: false,
        style: { stroke: "var(--vsc-fg-muted)" },
      },
      {
        id: "e-assistant-to-user",
        source: "assistant-root",
        target: "user-root",
        animated: false,
        style: { stroke: "var(--vsc-fg-muted)" },
      },
    ]);
    lastUserId.current = "user-root";
    lastAssistantId.current = "assistant-root";
  };

  const addMindMapNode = ({
    speaker,
    text,
    functionCall,
    parentId,
    toolStatus,
  }) => {
    const isTool = !!functionCall;

    if (!isTool) {
      // update root label
      const rootId = speaker === "User" ? "user-root" : "assistant-root";
      setNodes((nds) =>
        nds.map((n) =>
          n.id === rootId
            ? {
                ...n,
                data: {
                  ...n.data,
                  label: text.length > 40 ? text.slice(0, 37) + "…" : text,
                },
                style: {
                  ...n.style,
                  border: "2px solid var(--vsc-warning)",
                  animation: "pulseNode 1.6s ease-out infinite",
                },
              }
            : n
        )
      );
      if (speaker === "User") lastUserId.current = rootId;
      else lastAssistantId.current = rootId;
      return rootId;
    }

    // tool node
    const newId = nextId();
    const toolCount = nodes.filter((n) => n.data.speaker === "Tool").length;
    const toolNode = {
      id: newId,
      data: {
        speaker: "Tool",
        label: `🛠️ ${functionCall} ${
          toolStatus === "running"
            ? "🔄"
            : toolStatus === "completed"
            ? "✔️"
            : "❌"
        }`,
      },
      position: { x: 400, y: toolCount * 80 + 50 },
      style: {
        background: "var(--vsc-warning)",
        color: "var(--vsc-editor-bg)",
        padding: 8,
        fontSize: 12,
        width: 200,
        height: 60,
        borderRadius: 8,
      },
    };
    const edge = {
      id: `e-${parentId}-${newId}`,
      source: parentId,
      target: newId,
      animated: true,
      style: { stroke: "var(--vsc-warning)", strokeDasharray: "4 2" },
    };

    setNodes((nds) => [...nds, toolNode]);
    setEdges((eds) => [...eds, edge]);
    return newId;
  };

  return {
    nodes,
    edges,
    setNodes,
    setEdges,
    resetMindMap,
    addMindMapNode,
    lastUserId,
    lastAssistantId,
  };
}
