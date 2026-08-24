import React, { useState, useEffect, useMemo, useCallback, memo } from 'react';
import {
  HistoryOutlined,
  AccessTimeOutlined,
  SmartToyOutlined,
  PlayCircleOutlined,
  DeleteOutlined,
  RefreshOutlined,
  ErrorOutlineOutlined
} from '@mui/icons-material';
import { API_BASE_URL } from '../config/constants.js';
import logger from '../utils/logger.js';

const SESSION_STORAGE_KEY = 'voice_agent_session_id';

const styles = {
  container: {
    position: 'fixed',
    bottom: '32px',
    left: '32px',
    zIndex: 11000,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-start',
    pointerEvents: 'none',
  },
  toggleButton: (open) => ({
    pointerEvents: 'auto',
    border: open ? '1px solid rgba(var(--vsc-accent-rgb),0.42)' : '1px solid var(--vsc-border)',
    outline: 'none',
    minHeight: '40px',
    borderRadius: '8px',
    background: open
      ? 'var(--vsc-accent)'
      : 'rgba(255,255,255,0.06)',
    color: open ? 'var(--vsc-fg-strong)' : 'var(--vsc-fg)',
    padding: '0 13px',
    fontWeight: 800,
    fontSize: '12px',
    letterSpacing: '0.02em',
    cursor: 'pointer',
    boxShadow: open ? '0 8px 18px rgba(var(--vsc-accent-rgb),0.28)' : '0 10px 24px rgba(0,0,0,0.20)',
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    transition: 'background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease, color 140ms ease',
  }),
  iconBadge: {
    width: '24px',
    height: '24px',
    borderRadius: '6px',
    background: 'rgba(var(--vsc-accent-rgb),0.18)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: '16px',
  },
  panel: {
    pointerEvents: 'auto',
    width: '320px',
    maxWidth: 'calc(100vw - 48px)',
    maxHeight: '70vh',
    background: 'var(--vsc-panel-bg)',
    color: 'var(--vsc-fg-strong)',
    borderRadius: '8px',
    padding: '18px',
    marginBottom: '12px',
    boxShadow: '0 10px 24px rgba(0,0,0,0.32)',
    border: '1px solid var(--vsc-border)',
    backdropFilter: 'blur(16px)',
    transition: 'opacity 0.2s ease, transform 0.2s ease',
    overflowY: 'auto',
    scrollbarWidth: 'none',
    msOverflowStyle: 'none',
  },
  panelHidden: {
    opacity: 0,
    transform: 'translateY(10px)',
    pointerEvents: 'none',
  },
  panelVisible: {
    opacity: 1,
    transform: 'translateY(0)',
  },
  panelHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '16px',
  },
  panelHeaderActions: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
  },
  headerButton: {
    border: '1px solid var(--vsc-border)',
    background: 'rgba(255, 255, 255, 0.08)',
    color: 'rgba(var(--vsc-accent-rgb), 0.20)',
    width: '28px',
    height: '28px',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '14px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  panelTitle: {
    fontSize: '14px',
    fontWeight: 700,
    letterSpacing: '0.8px',
    textTransform: 'uppercase',
  },
  closeButton: {
    border: '1px solid var(--vsc-border)',
    background: 'rgba(255, 255, 255, 0.08)',
    color: 'rgba(var(--vsc-accent-rgb), 0.20)',
    width: '28px',
    height: '28px',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '14px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  emptyState: {
    textAlign: 'center',
    padding: '24px 16px',
    color: 'var(--vsc-fg-muted)',
  },
  emptyIcon: {
    fontSize: '48px',
    color: 'rgba(148, 163, 184, 0.3)',
    marginBottom: '12px',
  },
  emptyText: {
    fontSize: '14px',
    fontWeight: 500,
    marginBottom: '8px',
  },
  emptySubtext: {
    fontSize: '11px',
    lineHeight: 1.4,
  },
  sessionsList: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  },
  sessionCard: (isActive) => ({
    background: isActive
      ? 'rgba(139, 92, 246, 0.15)'
      : 'rgba(15, 23, 42, 0.75)',
    borderRadius: '8px',
    padding: '12px',
    border: isActive
      ? '1px solid rgba(139, 92, 246, 0.4)'
      : '1px solid rgba(255, 255, 255, 0.08)',
    cursor: 'pointer',
    transition: 'all 0.2s ease',
    position: 'relative',
  }),
  sessionCardHover: {
    background: 'rgba(139, 92, 246, 0.1)',
    borderColor: 'rgba(139, 92, 246, 0.3)',
  },
  sessionHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: '8px',
  },
  sessionId: {
    fontSize: '11px',
    fontWeight: 600,
    color: 'var(--vsc-fg)',
    fontFamily: 'Monaco, Menlo, monospace',
    flex: 1,
    marginRight: '8px',
  },
  sessionActions: {
    display: 'flex',
    gap: '4px',
  },
  actionButton: {
    border: 'none',
    background: 'rgba(255, 255, 255, 0.1)',
    color: 'rgba(var(--vsc-accent-rgb), 0.20)',
    width: '24px',
    height: '24px',
    borderRadius: '6px',
    cursor: 'pointer',
    fontSize: '12px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'all 0.15s ease',
  },
  actionButtonHover: {
    background: 'rgba(255, 255, 255, 0.2)',
  },
  deleteButton: {
    color: 'var(--vsc-error)',
  },
  sessionMeta: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
  },
  sessionTime: {
    fontSize: '10px',
    color: 'var(--vsc-fg-muted)',
    display: 'flex',
    alignItems: 'center',
    gap: '4px',
  },
  sessionAgents: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '4px',
  },
  agentChip: {
    display: 'inline-flex',
    alignItems: 'center',
    padding: '2px 6px',
    borderRadius: '6px',
    fontSize: '9px',
    fontWeight: 600,
    letterSpacing: '0.2px',
    background: 'rgba(103, 216, 239, 0.2)',
    color: 'var(--vsc-link)',
    border: '1px solid rgba(103, 216, 239, 0.3)',
  },
  scenarioChip: {
    display: 'inline-flex',
    alignItems: 'center',
    padding: '2px 6px',
    borderRadius: '6px',
    fontSize: '9px',
    fontWeight: 600,
    letterSpacing: '0.2px',
    background: 'rgba(34, 197, 94, 0.2)',
    color: 'var(--vsc-success)',
    border: '1px solid rgba(34, 197, 94, 0.3)',
  },
  currentBadge: {
    position: 'absolute',
    top: '-4px',
    right: '-4px',
    background: 'linear-gradient(135deg, var(--vsc-accent), var(--vsc-accent))',
    color: 'var(--vsc-fg-strong)',
    fontSize: '8px',
    fontWeight: 700,
    padding: '2px 6px',
    borderRadius: '6px',
    textTransform: 'uppercase',
    letterSpacing: '0.3px',
  },
  refreshButton: {
    width: '100%',
    background: 'rgba(255, 255, 255, 0.08)',
    border: '1px solid rgba(255, 255, 255, 0.12)',
    color: 'rgba(var(--vsc-accent-rgb), 0.20)',
    padding: '8px 12px',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '11px',
    fontWeight: 600,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '6px',
    marginTop: '12px',
    transition: 'all 0.2s ease',
  },
  customList: {
    display: 'flex',
    flexDirection: 'column',
    gap: '2px',
    marginBottom: '6px',
  },
  customRow: {
    display: 'flex',
    gap: '6px',
    fontSize: '9px',
    color: 'var(--vsc-fg-muted)',
  },
  customLabel: {
    fontWeight: 600,
    color: 'rgba(var(--vsc-accent-rgb), 0.20)',
  },
  customValue: {
    color: 'var(--vsc-fg-muted)',
    flex: 1,
  },
};

const toTimestampMs = (value) => {
  if (value === null || value === undefined) {
    return null;
  }

  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000;
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) {
      return null;
    }
    const numeric = Number(trimmed);
    if (!Number.isNaN(numeric)) {
      return numeric > 1e12 ? numeric : numeric * 1000;
    }
    const parsed = Date.parse(trimmed);
    return Number.isNaN(parsed) ? null : parsed;
  }

  if (value instanceof Date) {
    const time = value.getTime();
    return Number.isNaN(time) ? null : time;
  }

  return null;
};

const SessionSelector = ({ onSessionChange }) => {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [hoveredSession, setHoveredSession] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Memoized session data transformer
  const transformSessionData = useCallback((sessionData) => {
    const lastActivityMs = toTimestampMs(sessionData.last_activity);
    const createdAtMs = toTimestampMs(sessionData.created_at);
    const resolvedTimestamp = lastActivityMs ?? createdAtMs ?? 0;

    return {
      sessionId: sessionData.session_id,
      timestamp: resolvedTimestamp,
      agents: sessionData.agents?.map(agent => agent.name) || [],
      activeAgent: sessionData.agents?.find(agent => agent.is_active)?.name || null,
      scenario: sessionData.scenarios?.find(scenario => scenario.is_active)?.name || null,
      connectionStatus: sessionData.connection_status || 'inactive',
      turnCount: sessionData.turn_count || 0,
      lastActivity: lastActivityMs ?? createdAtMs ?? null,
      lastActivityReadable: sessionData.last_activity_readable || '',
      userEmail: sessionData.user_email || null,
      streamingMode: sessionData.streaming_mode || null,
      hasCustomAgents: sessionData.has_custom_agents || false,
      hasCustomScenarios: sessionData.has_custom_scenarios || false,
      profileName: sessionData.profile_name || null,
      profileType: sessionData.profile_type || null,
      agentsCount: sessionData.agents_count || 0,
      scenariosCount: sessionData.scenarios_count || 0,
      activeAgentsCount: sessionData.active_agents_count || 0,
      customAgentsCount: sessionData.custom_agents_count || 0,
      customScenariosCount: sessionData.custom_scenarios_count || 0,
      allAgents: sessionData.agents || [],
      allScenarios: sessionData.scenarios || []
    };
  }, []);

  const fetchActiveSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/sessions?limit=50`);
      if (!response.ok) {
        throw new Error(`Failed to fetch sessions: ${response.status}`);
      }
      const data = await response.json();

      // Transform backend data to our format using memoized transformer
      const sessionList = (data.sessions || []).map(transformSessionData);

      setSessions(sessionList);
      logger.info(`Loaded ${sessionList.length} sessions from backend (total: ${data.total_count}, active: ${data.active_count})`);
    } catch (error) {
      logger.error('Failed to fetch sessions from backend:', error);
      setError(error.message);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, [transformSessionData]);

  const getSessionDetails = async (sessionId) => {
    try {
      // Use the new detailed session endpoint
      const response = await fetch(`${API_BASE_URL}/api/v1/sessions/${encodeURIComponent(sessionId)}?include_history=true&include_memory=true`);

      if (!response.ok) {
        throw new Error(`Failed to fetch session details: ${response.status}`);
      }

      const data = await response.json();

      const sessionDetails = {
        agents: data.session?.agents?.map(agent => agent.name) || [],
        scenarios: data.session?.scenarios?.map(scenario => scenario.name) || [],
        agentConfig: data.agent_configs || null,
        scenarioConfig: data.scenario_configs || null,
        chatHistory: data.chat_history || [],
        memory: data.memory || {},
        session: data.session || null
      };

      return sessionDetails;
    } catch (error) {
      logger.error(`Failed to fetch session details for ${sessionId}:`, error);
      return { agents: [], scenarios: [], agentConfig: null, scenarioConfig: null, chatHistory: [], memory: {}, session: null };
    }
  };

  const selectSession = useCallback(async (sessionId) => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    setCurrentSessionId(sessionId);

    // Fetch detailed session information when selecting
    const details = await getSessionDetails(sessionId);

    if (onSessionChange) {
      onSessionChange(sessionId, details);
    }
    setOpen(false);
  }, [onSessionChange]);

  const deleteSession = useCallback(async (sessionId, event) => {
    event.stopPropagation();

    try {
      // Call backend to delete session from Redis
      const response = await fetch(`${API_BASE_URL}/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE'
      });

      if (!response.ok) {
        throw new Error(`Failed to delete session: ${response.status}`);
      }

      // Remove from local state
      setSessions(prevSessions => prevSessions.filter(s => s.sessionId !== sessionId));

      if (currentSessionId === sessionId) {
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
        setCurrentSessionId(null);
        if (onSessionChange) {
          onSessionChange(null);
        }
      }

      const result = await response.json();
      logger.info(`Session ${sessionId} deleted from Redis:`, result);
    } catch (error) {
      logger.error('Failed to delete session:', error);
      setError(`Failed to delete session: ${error.message}`);
    }
  }, [currentSessionId, onSessionChange]);

  const formatTimestamp = useCallback((timestamp, fallback) => {
    if (timestamp) {
      const date = new Date(timestamp);
      if (!Number.isNaN(date.getTime())) {
        return date.toLocaleString(undefined, {
          month: 'short',
          day: '2-digit',
          year: 'numeric',
          hour: 'numeric',
          minute: '2-digit'
        });
      }
    }

    if (fallback && fallback !== 'Just now') {
      return fallback;
    }

    return 'Activity unknown';
  }, []);

  useEffect(() => {
    // Initialize current session from sessionStorage
    const current = sessionStorage.getItem(SESSION_STORAGE_KEY);
    setCurrentSessionId(current);

    const handleStorageChange = (e) => {
      if (e.key === SESSION_STORAGE_KEY) {
        const newSessionId = sessionStorage.getItem(SESSION_STORAGE_KEY);
        setCurrentSessionId(newSessionId);
      }
    };

    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, []);

  // Only fetch sessions when panel is opened for the first time or manually refreshed
  useEffect(() => {
    if (open && sessions.length === 0 && !loading) {
      fetchActiveSessions();
    }
  }, [open, sessions.length, loading, fetchActiveSessions]);

  // Auto-refresh sessions periodically - increased to 60 seconds for performance
  useEffect(() => {
    const interval = setInterval(() => {
      // Only refresh if the panel is open and not currently loading
      if (open && !loading) {
        fetchActiveSessions();
      }
    }, 60000); // Refresh every 60 seconds, only when panel is open

    return () => clearInterval(interval);
  }, [open, loading, fetchActiveSessions]);

  const sortedSessions = useMemo(() => {
    return [...sessions].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  }, [sessions]);

  const togglePanel = useCallback(() => setOpen(prev => !prev), []);

  // Memoized session card component for better performance
  const SessionCard = memo(({ session, isActive, isHovered, onSelect, onDelete, onHover }) => {
    const inactiveAgents = useMemo(() => {
      return session.agents?.filter(agent => agent !== session.activeAgent) || [];
    }, [session.agents, session.activeAgent]);

    const visibleInactiveAgents = useMemo(() => {
      return inactiveAgents.slice(0, 2);
    }, [inactiveAgents]);

    const extraAgentCount = useMemo(() => {
      return inactiveAgents.length > 2 ? inactiveAgents.length - 2 : 0;
    }, [inactiveAgents.length]);

    const customAgents = useMemo(() => {
      return (session.allAgents || [])
        .filter(agent => agent?.is_custom)
        .map(agent => agent?.name)
        .filter(Boolean);
    }, [session.allAgents]);

    const scenarioNames = useMemo(() => {
      return (session.allScenarios || [])
        .map(scenario => scenario?.name)
        .filter(Boolean);
    }, [session.allScenarios]);

    const showScenarioNames = useMemo(() => {
      return session.customScenariosCount > 0 && scenarioNames.length > 0;
    }, [session.customScenariosCount, scenarioNames.length]);

    const summaryAgentsCount = Number.isFinite(session.agentsCount) ? session.agentsCount : 0;
    const summaryScenariosCount = Number.isFinite(session.scenariosCount) ? session.scenariosCount : 0;
    const summaryTurns = Number.isFinite(session.turnCount) ? session.turnCount : 0;

    return (
      <div
        style={{
          ...styles.sessionCard(isActive),
          ...(isHovered && !isActive ? styles.sessionCardHover : {})
        }}
        onClick={() => onSelect(session.sessionId)}
        onMouseEnter={() => onHover(session.sessionId)}
        onMouseLeave={() => onHover(null)}
      >
        {isActive && <div style={styles.currentBadge}>Current</div>}

        <div style={styles.sessionHeader}>
          <div style={styles.sessionId}>
            {session.sessionId.replace('session_', '')}
          </div>
          <div style={styles.sessionActions}>
            <button
              style={{
                ...styles.actionButton,
                ...(isHovered ? styles.actionButtonHover : {})
              }}
              onClick={(e) => {
                e.stopPropagation();
                onSelect(session.sessionId);
              }}
              title="Load session"
            >
              <PlayCircleOutlined style={{ fontSize: 12 }} />
            </button>
            <button
              style={{
                ...styles.actionButton,
                ...styles.deleteButton,
                ...(isHovered ? styles.actionButtonHover : {})
              }}
              onClick={(e) => onDelete(session.sessionId, e)}
              title="Delete session"
            >
              <DeleteOutlined style={{ fontSize: 12 }} />
            </button>
          </div>
        </div>

        {/* Profile info */}
        {session.profileName && (
          <div style={{
            fontSize: '10px',
            fontWeight: 600,
            color: 'var(--vsc-accent)',
            marginBottom: '4px',
            display: 'flex',
            alignItems: 'center',
            gap: '4px'
          }}>
            <span style={{ fontSize: '8px' }}>👤</span>
            {session.profileName}
            {session.profileType && (
              <span style={{
                fontSize: '8px',
                color: 'var(--vsc-fg-muted)',
                fontWeight: 400,
                textTransform: 'capitalize'
              }}>
                ({session.profileType})
              </span>
            )}
          </div>
        )}

        {/* Counts summary */}
        <div style={{
          fontSize: '9px',
          color: 'var(--vsc-fg-muted)',
          marginBottom: '6px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px'
        }}>
          <span>🤖 {summaryAgentsCount} agent{summaryAgentsCount !== 1 ? 's' : ''}</span>
          <span>🎭 {summaryScenariosCount} scenario{summaryScenariosCount !== 1 ? 's' : ''}</span>
          <span>💬 {summaryTurns} turn{summaryTurns !== 1 ? 's' : ''}</span>
        </div>

        <div style={styles.sessionMeta}>
          <div style={styles.sessionTime}>
            <AccessTimeOutlined style={{ fontSize: 10 }} />
            {formatTimestamp(session.lastActivity, session.lastActivityReadable)}
          </div>

          {(customAgents.length > 0 || showScenarioNames || session.customAgentsCount > 0 || session.customScenariosCount > 0) && (
            <div style={styles.customList}>
              {customAgents.length > 0 ? (
                <div style={styles.customRow}>
                  <span style={styles.customLabel}>Active agents</span>
                  <span style={styles.customValue}>
                    {customAgents.slice(0, 3).join(', ')}
                    {customAgents.length > 3 ? ` +${customAgents.length - 3} more` : ''}
                  </span>
                </div>
              ) : session.customAgentsCount > 0 ? (
                <div style={styles.customRow}>
                  <span style={styles.customLabel}>Custom agents</span>
                  <span style={styles.customValue}>{session.customAgentsCount}</span>
                </div>
              ) : null}

              {showScenarioNames ? (
                <div style={styles.customRow}>
                  <span style={styles.customLabel}>Custom scenarios</span>
                  <span style={styles.customValue}>
                    {scenarioNames.slice(0, 3).join(', ')}
                    {scenarioNames.length > 3 ? ` +${scenarioNames.length - 3} more` : ''}
                  </span>
                </div>
              ) : session.customScenariosCount > 0 ? (
                <div style={styles.customRow}>
                  <span style={styles.customLabel}>Custom scenarios</span>
                  <span style={styles.customValue}>{session.customScenariosCount}</span>
                </div>
              ) : null}
            </div>
          )}

          {(session.agents?.length > 0 || session.scenario || session.activeAgent) && (
            <div style={styles.sessionAgents}>
              {session.activeAgent && (
                <span style={styles.agentChip}>
                  <SmartToyOutlined style={{ fontSize: 8, marginRight: 2 }} />
                  {session.activeAgent}
                </span>
              )}
              {visibleInactiveAgents.map((agent, idx) => (
                <span key={idx} style={{...styles.agentChip, opacity: 0.7}}>
                  <SmartToyOutlined style={{ fontSize: 8, marginRight: 2 }} />
                  {agent}
                </span>
              ))}
              {extraAgentCount > 0 && (
                <span style={{...styles.agentChip, opacity: 0.5}}>
                  +{extraAgentCount} more
                </span>
              )}
              {session.scenario && (
                <span style={styles.scenarioChip}>
                  {session.scenario}
                </span>
              )}
              {session.customAgentsCount > 0 && (
                <span style={{
                  ...styles.agentChip,
                  background: 'rgba(139, 92, 246, 0.2)',
                  color: 'var(--vsc-accent)',
                  border: '1px solid rgba(139, 92, 246, 0.3)',
                }}>
                  {session.customAgentsCount} custom
                </span>
              )}
              {session.customScenariosCount > 0 && (
                <span style={{
                  ...styles.scenarioChip,
                  background: 'rgba(245, 158, 11, 0.2)',
                  color: 'var(--vsc-warning)',
                  border: '1px solid rgba(245, 158, 11, 0.3)',
                }}>
                  {session.customScenariosCount} custom
                </span>
              )}
              {session.userEmail && (
                <span style={{
                  ...styles.agentChip,
                  background: 'rgba(34, 197, 94, 0.1)',
                  color: 'var(--vsc-success)',
                  border: '1px solid rgba(34, 197, 94, 0.2)',
                }}>
                  {session.userEmail.split('@')[0]}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    );
  });

  // Memoized hover handler
  const handleHover = useCallback((sessionId) => {
    setHoveredSession(sessionId);
  }, []);

  // Create a render function for the optimized session card
  const renderSessionCard = useCallback((session) => (
    <SessionCard
      key={session.sessionId}
      session={session}
      isActive={session.sessionId === currentSessionId}
      isHovered={hoveredSession === session.sessionId}
      onSelect={selectSession}
      onDelete={deleteSession}
      onHover={handleHover}
    />
  ), [currentSessionId, hoveredSession, selectSession, deleteSession, handleHover]);

  return (
    <div style={styles.container} aria-live="polite">
      <style>{`
        .session-selector-panel::-webkit-scrollbar { display: none; }
        .session-selector-panel { scrollbar-width: none; -ms-overflow-style: none; }

        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>

      {open && (
        <div
          className="session-selector-panel"
          style={{
            ...styles.panel,
            ...styles.panelVisible,
          }}
          role="dialog"
          aria-label="Session selector"
          aria-hidden={!open}
        >
          <div style={styles.panelHeader}>
            <div style={styles.panelTitle}>Session History</div>
            <div style={styles.panelHeaderActions}>
              <button
                type="button"
                style={styles.headerButton}
                aria-label="Refresh sessions"
                onClick={fetchActiveSessions}
                disabled={loading}
                title="Refresh sessions"
              >
                <RefreshOutlined style={{ fontSize: 14, animation: loading ? 'spin 1s linear infinite' : 'none' }} />
              </button>
              <button
                type="button"
                style={styles.closeButton}
                aria-label="Close session selector"
                onClick={togglePanel}
              >
                ×
              </button>
            </div>
          </div>

          {loading ? (
            <div style={styles.emptyState}>
              <div style={styles.emptyIcon}>
                <RefreshOutlined style={{ fontSize: 48, animation: 'spin 1s linear infinite' }} />
              </div>
              <div style={styles.emptyText}>Loading sessions...</div>
              <div style={styles.emptySubtext}>
                Fetching active sessions from backend
              </div>
            </div>
          ) : error ? (
            <div style={styles.emptyState}>
              <div style={styles.emptyIcon}>
                <ErrorOutlineOutlined style={{ fontSize: 48, color: 'var(--vsc-error)' }} />
              </div>
              <div style={styles.emptyText}>Failed to load sessions</div>
              <div style={styles.emptySubtext}>
                {error}
              </div>
              <button
                style={{
                  ...styles.refreshButton,
                  marginTop: '12px',
                  background: 'rgba(239, 68, 68, 0.1)',
                  border: '1px solid rgba(239, 68, 68, 0.3)',
                  color: 'var(--vsc-error)'
                }}
                onClick={fetchActiveSessions}
                title="Retry loading sessions"
              >
                <RefreshOutlined style={{ fontSize: 12 }} />
                Retry
              </button>
            </div>
          ) : sortedSessions.length === 0 ? (
            <div style={styles.emptyState}>
              <div style={styles.emptyIcon}>
                <HistoryOutlined style={{ fontSize: 48 }} />
              </div>
              <div style={styles.emptyText}>No active sessions</div>
              <div style={styles.emptySubtext}>
                Start a conversation to create your first session
              </div>
            </div>
          ) : (
            <>
              <div style={styles.sessionsList}>
                {sortedSessions.map(renderSessionCard)}
              </div>

              <button
                style={styles.refreshButton}
                onClick={fetchActiveSessions}
                disabled={loading}
                title="Refresh sessions"
              >
                <RefreshOutlined style={{ fontSize: 12 }} />
                Refresh Sessions
              </button>
            </>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={togglePanel}
        style={styles.toggleButton(open)}
        aria-expanded={open}
        aria-label="Toggle session selector"
      >
        <span style={styles.iconBadge}>
          <HistoryOutlined style={{ fontSize: 16 }} />
        </span>
        <span>Sessions</span>
      </button>
    </div>
  );
};

export default SessionSelector;
