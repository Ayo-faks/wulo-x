import React from 'react';
import { styles } from '../styles/voiceAppStyles.js';

const IndustryTag = () => {
  const getIndustryPresentation = () => {
    const currentBranch = import.meta.env.VITE_BRANCH_NAME || 'finance';

    if (currentBranch === 'main') {
      return {
        label: 'Insurance Edition',
        palette: {
          background: 'linear-gradient(135deg, var(--vsc-link), var(--vsc-success))',
          color: 'var(--vsc-fg)',
          borderColor: 'rgba(14,165,233,0.35)',
          shadow: '0 12px 28px rgba(14,165,233,0.24)',
          textShadow: '0 1px 2px rgba(15,23,42,0.3)',
        },
      };
    }

    if (currentBranch.includes('finance') || currentBranch.includes('capitalmarkets')) {
      return {
        label: 'Banking Edition',
        palette: {
          background: 'linear-gradient(135deg, var(--vsc-accent), var(--vsc-accent))',
          color: 'var(--vsc-fg-strong)',
          borderColor: 'rgba(var(--vsc-accent-rgb),0.45)',
          shadow: '0 12px 28px rgba(var(--vsc-accent-rgb),0.25)',
          textShadow: '0 1px 2px rgba(30,64,175,0.4)',
        },
      };
    }

    return {
      label: 'Banking Edition',
      palette: {
        background: 'linear-gradient(135deg, var(--vsc-accent), var(--vsc-accent))',
        color: 'var(--vsc-fg-strong)',
        borderColor: 'rgba(var(--vsc-accent-rgb),0.45)',
        shadow: '0 12px 28px rgba(var(--vsc-accent-rgb),0.25)',
        textShadow: '0 1px 2px rgba(30,64,175,0.4)',
      },
    };
  };

  const { label, palette } = getIndustryPresentation();

  return (
    <div style={styles.topTabsContainer}>
      <div style={styles.topTab(true, palette)}>{label}</div>
    </div>
  );
};

export default IndustryTag;
