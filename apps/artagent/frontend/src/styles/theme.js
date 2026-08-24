import { createTheme } from '@mui/material/styles'

export const vscodeTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: {
      main: '#007acc',
      dark: '#0e639c',
      light: '#3794ff',
      contrastText: 'var(--vsc-fg-strong)',
    },
    success: {
      main: '#4ec9b0',
      contrastText: '#1e1e1e',
    },
    warning: {
      main: '#cca700',
      contrastText: '#1e1e1e',
    },
    error: {
      main: '#f14c4c',
      contrastText: 'var(--vsc-fg-strong)',
    },
    background: {
      default: '#1e1e1e',
      paper: '#252526',
    },
    text: {
      primary: '#cccccc',
      secondary: '#858585',
    },
    divider: '#3c3c3c',
  },
  typography: {
    fontFamily: "'Manrope', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  },
  shape: {
    borderRadius: 8,
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: {
          backgroundColor: 'var(--vsc-editor-bg)',
          color: 'var(--vsc-fg)',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          textTransform: 'none',
          borderRadius: 8,
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
          borderColor: 'var(--vsc-border)',
        },
      },
    },
  },
})