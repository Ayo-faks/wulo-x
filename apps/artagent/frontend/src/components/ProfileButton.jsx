import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Avatar,
  Typography,
  Box,
} from '@mui/material';

/* ------------------------------------------------------------------ *
 *  PROFILE BUTTON COMPONENT WITH MATERIAL UI
 * ------------------------------------------------------------------ */
const resolveRelationshipTier = (profileData) => (
  profileData?.relationship_tier
  || profileData?.customer_intelligence?.relationship_context?.relationship_tier
  || profileData?.customer_intelligence?.relationship_context?.tier
  || '—'
);

const getInitials = (name) => {
  if (!name) return 'U';
  return name.split(' ').map((n) => n[0]).join('').toUpperCase().slice(0, 2);
};

const getTierColor = (tier) => {
  switch (tier?.toLowerCase()) {
    case 'platinum':
      return 'var(--vsc-border)';
    case 'gold':
      return 'var(--vsc-warning)';
    case 'silver':
      return 'var(--vsc-fg-muted)';
    case 'bronze':
      return 'var(--vsc-warning)';
    default:
      return 'var(--vsc-fg-muted)';
  }
};

const ProfileButtonComponent = ({
  profile,
  onCreateProfile,
  onTogglePanel,
  highlight = false,
}) => {
  const [highlighted, setHighlighted] = useState(false);
  const lastProfileIdentityRef = useRef(null);
  const highlightTimeoutRef = useRef(null);

  const startHighlight = useCallback(() => {
    setHighlighted(true);
    if (highlightTimeoutRef.current) {
      clearTimeout(highlightTimeoutRef.current);
    }
    highlightTimeoutRef.current = window.setTimeout(() => {
      setHighlighted(false);
      highlightTimeoutRef.current = null;
    }, 3200);
  }, []);

  const handleClick = () => {
    if (!profile) {
      // If no profile, trigger profile creation
      onCreateProfile?.();
      return;
    }
    if (highlightTimeoutRef.current) {
      clearTimeout(highlightTimeoutRef.current);
      highlightTimeoutRef.current = null;
    }
    setHighlighted(false);
    onTogglePanel?.();
  };

  useEffect(() => {
    if (!profile) {
      lastProfileIdentityRef.current = null;
      if (highlightTimeoutRef.current) {
        clearTimeout(highlightTimeoutRef.current);
        highlightTimeoutRef.current = null;
      }
      setHighlighted(false);
      return () => {};
    }

    const identity =
      profile?.sessionId ||
      profile?.entryId ||
      profile?.profile?.id ||
      profile?.profile?.full_name ||
      profile?.profile?.email;

    if (!identity || lastProfileIdentityRef.current === identity) {
      return () => {};
    }

    lastProfileIdentityRef.current = identity;
    startHighlight();

    return () => {
      if (highlightTimeoutRef.current) {
        clearTimeout(highlightTimeoutRef.current);
        highlightTimeoutRef.current = null;
      }
    };
  }, [profile, startHighlight]);

  useEffect(() => {
    if (highlight) {
      startHighlight();
    }
  }, [highlight, startHighlight]);

  useEffect(() => () => {
    if (highlightTimeoutRef.current) {
      clearTimeout(highlightTimeoutRef.current);
    }
  }, []);

  // No profile state - button handled upstream
  if (!profile) {
    return null;
  }

  const profileData = profile.profile;
  if (!profileData) {
    return null;
  }
  const tier = resolveRelationshipTier(profileData);
  const ssnLast4 = profileData?.verification_codes?.ssn4 || '----';
  const institutionName = profileData?.institution_name || 'Demo Institution';
  const companyCode = profileData?.company_code;
  const companyCodeLast4 = profileData?.company_code_last4 || companyCode?.slice?.(-4) || '----';
  const institutionSnippet = institutionName?.length > 30
    ? `${institutionName.slice(0, 27)}…`
    : institutionName;

  return (
    <>
      {/* Compact Profile Button */}
      <Box 
        onClick={handleClick}
        sx={{ 
          position: 'relative',
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          minHeight: 40,
          padding: '0 12px',
          borderRadius: '8px',
          background: 'rgba(255,255,255,0.06)',
          border: '1px solid var(--vsc-border)',
          cursor: 'pointer',
          transition: 'background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease',
          boxShadow: '0 10px 24px rgba(0,0,0,0.20)',
          maxWidth: '160px',
          flexShrink: 0,
          marginLeft: '4px',
          animation: highlighted ? 'profileButtonPulse 1.5s ease-in-out 3' : 'none',
          '&:hover': {
            background: 'var(--vsc-input-bg)',
            transform: 'translateY(-1px)',
            boxShadow: '0 8px 18px rgba(0,0,0,0.24)'
          },
          '@keyframes profileButtonPulse': {
            '0%': {
              boxShadow: '0 0 0 0 rgba(103, 216, 239, 0.55)',
              transform: 'scale(1)'
            },
            '70%': {
              boxShadow: '0 0 0 8px rgba(103, 216, 239, 0)',
              transform: 'translateY(-1px)'
            },
            '100%': {
              boxShadow: '0 0 0 0 rgba(103, 216, 239, 0)',
              transform: 'scale(1)'
            }
          }
        }}
      >
        <Avatar 
          sx={{ 
            width: 24, 
            height: 24, 
            bgcolor: getTierColor(tier),
            color: tier?.toLowerCase() === 'platinum' ? 'var(--vsc-fg)' : 'var(--vsc-fg-strong)',
            fontSize: '10px',
            fontWeight: 600
          }}
        >
          {getInitials(profileData?.full_name)}
        </Avatar>
        <Box sx={{ overflow: 'hidden', minWidth: 0 }}>
          <Typography 
            sx={{ 
              fontSize: '11px',
              fontWeight: 600,
              color: 'var(--vsc-fg)',
              lineHeight: 1.2,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap'
            }}
          >
            {profileData?.full_name || 'Demo User'}
          </Typography>
          <Typography 
            sx={{ 
              fontSize: '9px',
              color: 'var(--vsc-fg-muted)',
              lineHeight: 1,
              display: 'flex',
              flexWrap: 'wrap',
              columnGap: '6px',
              rowGap: '2px',
              whiteSpace: 'normal'
            }}
            component="div"
          >
            <span style={{ fontWeight: 600 }}>{institutionSnippet}</span>
            <span style={{ opacity: 0.8 }}>Co · ***{companyCodeLast4}</span>
            <span style={{ opacity: 0.8 }}>SSN · ***{ssnLast4}</span>
          </Typography>
        </Box>
      </Box>

      {/* Panel moved to separate component */}
    </>
  );
};

const areProfileButtonPropsEqual = (prevProps, nextProps) => (
  prevProps.profile === nextProps.profile &&
  prevProps.highlight === nextProps.highlight &&
  prevProps.onCreateProfile === nextProps.onCreateProfile &&
  prevProps.onTogglePanel === nextProps.onTogglePanel
);

export default React.memo(ProfileButtonComponent, areProfileButtonPropsEqual);
