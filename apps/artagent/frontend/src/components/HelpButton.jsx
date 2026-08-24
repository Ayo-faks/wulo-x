import React, { useState } from 'react';
import { styles } from '../styles/voiceAppStyles.js';

const HelpButton = () => {
  const [isHovered, setIsHovered] = useState(false);
  const [isClicked, setIsClicked] = useState(false);

  const handleClick = (e) => {
    if (e.target.tagName !== 'A') {
      e.preventDefault();
      e.stopPropagation();
      setIsClicked(!isClicked);
    }
  };

  const handleMouseLeave = () => {
    setIsHovered(false);
  };

  return (
    <div
      style={{
        width: '44px',
        height: '44px',
        borderRadius: '8px',
        border: '1px solid var(--vsc-border)',
        background: 'rgba(255,255,255,0.06)',
        color: 'var(--vsc-link)',
        fontSize: '14px',
        fontWeight: '600',
        cursor: 'pointer',
        transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
        boxShadow: 'none',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        position: 'relative',
        ...(isHovered || isClicked ? {
          transform: 'translateY(-2px)',
          boxShadow: '0 8px 18px rgba(0,0,0,0.24)',
          background: 'var(--vsc-input-bg)',
        } : {}),
      }}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={handleMouseLeave}
      onClick={handleClick}
    >
      ?
      <div
        style={{
          ...styles.helpTooltip,
          ...((isHovered || isClicked) ? styles.helpTooltipVisible : {}),
          // Show inward from the right-side dock.
          top: '50%',
          left: 'auto',
          right: '64px',
          transform: (isHovered || isClicked) ? 'translateY(-50%)' : 'translateY(-50%) translateX(8px)',
          width: '340px',
          background: 'linear-gradient(145deg, rgba(32,38,41,0.98), rgba(18,24,29,0.96))',
          boxShadow: '0 20px 52px rgba(0,0,0,0.48), 0 0 0 1px rgba(255,255,255,0.12), inset 0 1px 0 rgba(255,255,255,0.08)',
          backdropFilter: 'blur(24px)',
          WebkitBackdropFilter: 'blur(24px)',
        }}
      >
        <div style={styles.helpTooltipTitle} />
        <div
          style={{
            ...styles.helpTooltipText,
            color: '#f6a09b',
            fontWeight: '600',
            fontSize: '12px',
            marginBottom: '12px',
            padding: '8px',
            backgroundColor: 'rgba(229,72,77,0.16)',
            borderRadius: '4px',
            border: '1px solid rgba(229,72,77,0.40)',
          }}
        >
          This is a demo available for Microsoft employees only.
        </div>
        <div style={{ ...styles.helpTooltipTitle, color: 'var(--vsc-fg-strong)' }}>🤖 Wulo-X Phone Assistant</div>
        <div style={styles.helpTooltipText}>
          ARTAgent is an accelerator that delivers a friction-free, AI-driven voice experience—whether callers dial a phone number, speak to an IVR, or click &quot;Call Me&quot; in a web app. Built entirely on Azure services, it provides a low-latency stack that scales on demand while keeping the AI layer fully under your control.
        </div>
        <div style={styles.helpTooltipText}>
          Design a single agent or orchestrate multiple specialist agents. The framework allows you to build your voice agent from scratch, incorporate memory, configure actions, and fine-tune your TTS and STT layers.
        </div>
        <div style={styles.helpTooltipText}>
          🤔 <strong>Try asking about:</strong> Transfer Agency DRIP liquidations, compliance reviews, fraud detection, or general inquiries.
        </div>
        <div style={styles.helpTooltipText}>
          📑{' '}
          <a
            href="https://microsoft.sharepoint.com/teams/rtaudioagent"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: '#6cb6ff',
              textDecoration: 'underline',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            Visit the Project Hub
          </a>{' '}
          for instructions, deep dives and more.
        </div>
        <div style={styles.helpTooltipText}>
          📧 Questions or feedback?{' '}
          <a
            href="mailto:rtvoiceagent@microsoft.com?subject=ARTAgent Feedback"
            style={{
              color: '#6cb6ff',
              textDecoration: 'underline',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            Contact the team
          </a>
        </div>
        {isClicked && (
          <div
            style={{
              textAlign: 'center',
              marginTop: '8px',
              fontSize: '10px',
              color: 'var(--vsc-fg-muted)',
              fontStyle: 'italic',
            }}
          >
            Click ? again to close
          </div>
        )}
      </div>
    </div>
  );
};

export default HelpButton;
