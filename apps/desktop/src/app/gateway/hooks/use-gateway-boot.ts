import { useEffect, useRef } from 'react'

import type { HermesConnection } from '@/global'
import { HermesGateway } from '@/hermes'
import { translateNow } from '@/i18n'
import { desktopDefaultCwd } from '@/lib/desktop-fs'
import { isGatewayReauthRequired, resolveGatewayWsUrl } from '@/lib/gateway-ws-url'
import {
  $desktopBoot,
  applyDesktopBootProgress,
  completeDesktopBoot,
  failDesktopBoot,
  setDesktopBootStep
} from '@/store/boot'
import {
  $gateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  ensureGatewayForProfile,
  pruneSecondaryGateways,
  reconnectSecondaryGateways,
  reportPrimaryGatewayState,
  setPrimaryGateway,
  touchSecondaryGateways
} from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey, touchActiveGatewayBackend } from '@/store/profile'
import {
  $activeSessionId,
  $attentionSessionIds,
  $connection,
  $currentCwd,
  $sessions,
  $workingSessionIds,
  ensureDefaultWorkspaceCwd,
  setConnection,
  setCurrentBranch,
  setCurrentCwd,
  setSessionsLoading
} from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

interface GatewayBootOptions {
  handleGatewayEvent: (event: RpcEvent) => void
  onConnectionReady: (
    connection: Awaited<ReturnType<NonNullable<typeof window.hermesDesktop>['getConnection']>> | null
  ) => void
  onGatewayReady: (gateway: HermesGateway | null) => void
  refreshHermesConfig: () => Promise<void>
  refreshSessions: () => Promise<void>
}

export function useGatewayBoot({
  handleGatewayEvent,
  onConnectionReady,
  onGatewayReady,
  refreshHermesConfig,
  refreshSessions
}: GatewayBootOptions) {
  const callbacksRef = useRef({
    handleGatewayEvent,
    onConnectionReady,
    onGatewayReady,
    refreshHermesConfig,
    refreshSessions
  })

  callbacksRef.current = {
    handleGatewayEvent,
    onConnectionReady,
    onGatewayReady,
    refreshHermesConfig,
    refreshSessions
  }

  useEffect(() => {
    let cancelled = false
    const rawDesktop = window.hermesDesktop

    const publish = (next: HermesConnection | null) => {
      callbacksRef.current.onConnectionReady(next)
      setConnection(next)
    }

    // -------------------------------------------------------------------------
    // Standalone mode (no Electron): window.hermesDesktop is undefined when the
    // web UI is served by `hermes dashboard` (or the Docker image) instead of
    // the Tauri/Electron desktop app. The IPC bridge powers desktop-only
    // features (boot progress overlay, exit notifications, power-resume
    // reconnect, OAuth ticket minting, window state, etc.) — none of which the
    // standalone dashboard needs. We synthesize a minimal stub that:
    //   - `getConnection()` returns a local HermesConnection pointing at the
    //     same origin as the dashboard, with authMode='token' so
    //     resolveGatewayWsUrl() falls back to conn.wsUrl (no OAuth mint).
    //   - Every other method is a safe no-op (optional chaining below).
    // The rest of the hook then runs unchanged, opening a plain WebSocket
    // against the backend's /ws endpoint. If the user wants full desktop
    // features they should launch `hermes desktop` instead.
    // -------------------------------------------------------------------------
    let desktop: typeof window.hermesDesktop
    let isStandalone = false
    if (rawDesktop) {
      desktop = rawDesktop
    } else {
      isStandalone = true
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const baseUrl = window.location.origin
      const wsUrl = `${proto}//${window.location.host}/ws`
      const localConn: HermesConnection = {
        baseUrl,
        isFullscreen: false,
        mode: 'local',
        authMode: 'token',
        nativeOverlayWidth: 0,
        source: 'local',
        token: '',
        wsUrl,
        logs: [],
        profile: 'default',
        windowButtonPosition: null
      }
      desktop = {
        getConnection: async () => localConn,
        revalidateConnection: async () => ({ ok: true, rebuilt: false }),
        touchBackend: async () => ({ ok: true }),
        getGatewayWsUrl: async () => wsUrl,
        getBootProgress: async () => ({
          error: null,
          fakeMode: false,
          message: 'Standalone web dashboard (no desktop IPC)',
          phase: 'standalone',
          progress: 100,
          running: false,
          timestamp: Date.now()
        }),
        getConnectionConfig: async () => ({
          envOverride: false,
          mode: 'local',
          profile: 'default',
          remoteAuthMode: 'token',
          remoteOauthConnected: false,
          remoteTokenPreview: null,
          remoteTokenSet: false,
          remoteUrl: baseUrl
        }),
        saveConnectionConfig: async () => desktop!.getConnectionConfig() as never,
        applyConnectionConfig: async () => desktop!.getConnectionConfig() as never,
        testConnectionConfig: async () => ({
          baseUrl,
          ok: true,
          version: 'standalone'
        }),
        probeConnectionConfig: async () => ({
          baseUrl,
          reachable: true,
          authMode: 'token',
          providers: [],
          version: 'standalone',
          error: null
        }),
        oauthLoginConnectionConfig: async () => ({
          ok: false,
          baseUrl,
          connected: false
        }),
        oauthLogoutConnectionConfig: async () => ({
          ok: true,
          baseUrl,
          connected: false
        }),
        profile: {
          get: async () => ({ profile: 'default' }),
          set: async () => ({ profile: 'default' })
        },
        api: async <T,>() => ({} as T),
        notify: async () => true,
        requestMicrophoneAccess: async () => false,
        readFileDataUrl: async () => '',
        readFileText: async () => ({ binary: false, language: 'text', mimeType: 'text/plain', path: '', text: '', truncated: false }),
        selectPaths: async () => [],
        writeClipboard: async () => true,
        saveImageFromUrl: async () => false,
        saveImageBuffer: async () => '',
        saveClipboardImage: async () => '',
        getPathForFile: () => '',
        normalizePreviewTarget: async () => null,
        watchPreviewFile: async () => ({ id: 'standalone' }),
        stopPreviewFileWatch: async () => true,
        openExternal: async () => undefined,
        fetchLinkTitle: async () => '',
        sanitizeWorkspaceCwd: async (cwd) => ({ cwd: cwd ?? '', sanitized: false }),
        settings: {
          getDefaultProjectDir: async () => ({ defaultLabel: 'standalone', dir: null, resolvedCwd: '' }),
          pickDefaultProjectDir: async () => ({ canceled: true, dir: null }),
          setDefaultProjectDir: async () => ({ dir: null })
        },
        revealLogs: async () => ({ ok: true, path: '' }),
        getRecentLogs: async () => ({ path: '', lines: [] }),
        readDir: async () => ({ path: '', entries: [] }),
        terminal: {
          dispose: async () => false,
          onData: () => () => undefined,
          onExit: () => () => undefined,
          resize: async () => false,
          start: async () => ({ id: 'standalone', shell: '', cwd: '' }),
          write: async () => false
        },
        onPreviewFileChanged: () => () => undefined,
        onBackendExit: () => () => undefined,
        onBootProgress: () => () => undefined,
        getBootstrapState: async () => ({ active: false, manifest: null, stages: {}, error: null, log: [], startedAt: null, completedAt: null, unsupportedPlatform: null }),
        resetBootstrap: async () => ({ ok: true }),
        repairBootstrap: async () => ({ ok: true }),
        cancelBootstrap: async () => ({ ok: true, cancelled: false }),
        onBootstrapEvent: () => () => undefined,
        getVersion: async () => ({ appVersion: 'standalone', electronVersion: '', nodeVersion: '', platform: 'web', hermesRoot: '' }),
        updates: {
          check: async () => ({ supported: false, message: 'Updates are not available in standalone web mode. Use `hermes update` from the terminal.' }),
          apply: async () => ({ ok: false, error: 'standalone_mode', message: 'Updates are not available in standalone web mode. Use `hermes update` from the terminal.' }),
          getBranch: async () => ({ branch: 'main' }),
          setBranch: async () => ({ branch: 'main' }),
          onProgress: () => () => undefined
        },
        uninstall: {
          summary: async () => ({ hermes_home: '', agent_installed: false, gui_installed: false, source_built_artifacts: [], packaged_app_paths: [], userdata_dir: '', userdata_exists: false, platform: 'web' }),
          run: async () => ({ ok: false, error: 'standalone_mode' })
        },
        themes: {
          fetchMarketplace: async () => ({ files: [] }),
          searchMarketplace: async () => []
        }
      } as typeof window.hermesDesktop

      // Surface a non-blocking notice so users know they're in standalone mode.
      // We do this once per mount, not every reconnect.
      notify({
        kind: 'info',
        title: 'Nexus Web Dashboard (standalone)',
        body: 'You are running the dashboard in a browser, not the desktop app. Some desktop-only features (system file pickers, native notifications, exit-on-close handling) are unavailable. Run `hermes desktop` for the full experience.',
        silent: false,
        durationMs: 8000
      })
    }

    if (!desktop) {
      // Defensive: if even the stub failed to construct, fall back to the
      // original failure path. Should never happen because the stub is fully
      // local, but keeps the old behaviour in case of future regressions.
      failDesktopBoot('Desktop IPC bridge is unavailable.')
      setSessionsLoading(false)
      void isStandalone
      return () => void (cancelled = true)
    }

    // --- Reconnect-after-sleep machinery -------------------------------------
    // macOS sleep silently drops the renderer's WebSocket. The backend Python
    // process keeps running, but nothing re-opened the socket on wake, so the
    // composer stayed disabled forever on "Starting Hermes...". Once the
    // initial boot succeeds we treat any non-open state as recoverable and
    // reconnect with backoff, and we nudge a reconnect on the OS/browser
    // signals that fire around wake (power resume, network online, the window
    // becoming visible).
    let bootCompleted = false
    let reconnecting = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectAttempt = 0
    // Surface "sign in again" once per disconnect episode, not on every backoff
    // tick — a stale OAuth ticket fails every attempt and would otherwise stack
    // identical error toasts (and their haptics). Reset on the next clean open.
    let reauthNotified = false

    // Wrap the live getter in a call so TS control-flow analysis doesn't narrow
    // `connectionState` to a constant across the early-return guards (the state
    // genuinely changes between reads).
    const gatewayOpen = () => gateway.connectionState === 'open'

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const attemptReconnect = async () => {
      if (cancelled || reconnecting || gatewayOpen()) {
        return
      }

      reconnecting = true

      try {
        // Drop a stale REMOTE backend cache before re-dialing. After sleep/wake a
        // remote backend can become unreachable, but it has no child process
        // whose 'exit' would clear the main process's cached descriptor — without
        // this the renderer re-dials the same dead endpoint forever and stays on
        // "Starting Hermes…". The probe is a no-op for a healthy or local backend.
        await desktop.revalidateConnection?.().catch(() => undefined)

        const conn = await desktop.getConnection($activeGatewayProfile.get())

        if (cancelled) {
          return
        }

        publish(conn)
        // Re-mint the WS URL before reconnecting. OAuth tickets are single-use
        // with a short TTL, so the ticket baked into the cached conn.wsUrl is
        // dead on every reconnect after the initial boot — reusing it surfaces
        // as an opaque "Could not connect to Hermes gateway". resolveGatewayWsUrl
        // mints a fresh ticket (or throws a reauth error in OAuth mode rather
        // than connecting with a stale one). For local/token gateways the URL
        // carries a long-lived token and the re-mint is a cheap no-op.
        const wsUrl = await resolveGatewayWsUrl(desktop, conn)
        await gateway.connect(wsUrl)

        if (cancelled) {
          return
        }

        reconnectAttempt = 0
        // Resync state that may have moved on the backend while we were asleep.
        await callbacksRef.current.refreshHermesConfig().catch(() => undefined)
        await callbacksRef.current.refreshSessions().catch(() => undefined)
      } catch (err) {
        // OAuth session expired mid-reconnect: surface the actionable "sign in
        // again" message once instead of silently looping the backoff against a
        // ticket that can never succeed. Transport failures fall through to the
        // backoff in the finally block below.
        if (!cancelled && isGatewayReauthRequired(err) && !reauthNotified) {
          reauthNotified = true
          notifyError(err, translateNow('boot.errors.gatewaySignInRequired'))
        }
      } finally {
        reconnecting = false

        if (!cancelled && !gatewayOpen()) {
          scheduleReconnect()
        }
      }
    }

    function scheduleReconnect() {
      if (cancelled || reconnecting || reconnectTimer !== null || gatewayOpen()) {
        return
      }

      // 1s, 2s, 4s … capped at 15s.
      const delay = Math.min(15_000, 1_000 * 2 ** Math.min(reconnectAttempt, 4))
      reconnectAttempt += 1
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        void attemptReconnect()
      }, delay)
    }

    const reconnectNow = () => {
      if (cancelled || !bootCompleted) {
        return
      }

      clearReconnectTimer()
      reconnectAttempt = 0
      reconnectSecondaryGateways()

      if (!gatewayOpen()) {
        void attemptReconnect()
      }
    }

    const offBootProgress = desktop.onBootProgress(payload => applyDesktopBootProgress(payload))
    void desktop
      .getBootProgress()
      .then(snapshot => applyDesktopBootProgress(snapshot))
      .catch(() => undefined)

    setDesktopBootStep({
      phase: 'renderer.boot',
      message: translateNow('boot.steps.startingDesktopConnection'),
      progress: 6
    })

    const gateway = new HermesGateway()
    callbacksRef.current.onGatewayReady(gateway)
    setPrimaryGateway(gateway, normalizeProfileKey($activeGatewayProfile.get()))
    // Secondary (background-profile) sockets funnel into the same handler.
    configureGatewayRegistry({ onEvent: event => callbacksRef.current.handleGatewayEvent(event) })

    const offState = gateway.onState(st => {
      // Mirror to the composer only while the primary is the active profile —
      // a background secondary reconnect mustn't flip the foreground state.
      reportPrimaryGatewayState(st)

      if (st === 'open') {
        reconnectAttempt = 0
        reauthNotified = false
        clearReconnectTimer()

        // A revalidate-driven reconnect can rebuild the backend in place when the
        // cached remote was found dead, which re-drives the boot-progress overlay.
        // Unlike the initial boot, nothing calls completeDesktopBoot() afterwards,
        // so dismiss it here once we're open again — otherwise the overlay sticks
        // at ~94%. A no-op on a normal (non-rebuild) reconnect.
        if (bootCompleted) {
          completeDesktopBoot()
        }
      } else if (bootCompleted && (st === 'closed' || st === 'error')) {
        // The socket dropped after a healthy boot (typically sleep/wake). Try
        // to bring it back instead of leaving the composer stuck disabled.
        scheduleReconnect()
      }
    })

    const offEvent = gateway.onEvent(event => callbacksRef.current.handleGatewayEvent(event))

    // Wake signals: power resume (macOS/Windows), network coming back, and the
    // window regaining focus/visibility. Each nudges an immediate reconnect.
    const offPowerResume = desktop.onPowerResume?.(() => reconnectNow())

    const onOnline = () => reconnectNow()

    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        reconnectNow()
      }
    }

    window.addEventListener('online', onOnline)
    document.addEventListener('visibilitychange', onVisible)

    // Keep live pool backends alive while this window is open (the main process
    // can't observe the direct renderer↔backend WS). No-op for the primary.
    const keepaliveTimer = setInterval(() => {
      touchActiveGatewayBackend()
      touchSecondaryGateways()
    }, 60_000)

    // Bound concurrency cost to live work: keep a background socket only while
    // its profile has a running (working) or blocked (needs-input) session.
    // Once that profile goes idle its socket is dropped and its backend is free
    // to idle-reap. The active profile is always spared.
    const recomputeKeptGateways = () => {
      const live = new Set([...$workingSessionIds.get(), ...$attentionSessionIds.get()])
      const keep = new Set<string>()

      for (const session of $sessions.get()) {
        if (live.has(session.id)) {
          keep.add(normalizeProfileKey(session.profile))
        }
      }

      pruneSecondaryGateways(keep)
    }

    const offWorking = $workingSessionIds.subscribe(() => recomputeKeptGateways())
    const offAttention = $attentionSessionIds.subscribe(() => recomputeKeptGateways())
    const offActiveProfile = $activeGatewayProfile.subscribe(() => recomputeKeptGateways())

    const offWindowState = desktop.onWindowStateChanged?.(payload => {
      const current = $connection.get()

      if (current) {
        publish({ ...current, ...payload })
      }
    })

    const offExit = desktop.onBackendExit(() => {
      if ($desktopBoot.get().running || $desktopBoot.get().visible) {
        failDesktopBoot(translateNow('boot.errors.backgroundExitedDuringStartup'))
      }

      notify({
        kind: 'error',
        title: translateNow('boot.errors.backendStopped'),
        message: translateNow('boot.errors.backgroundExited'),
        durationMs: 0
      })
    })

    async function boot() {
      try {
        const conn = await desktop.getConnection()

        if (cancelled) {
          return
        }

        setDesktopBootStep({
          phase: 'renderer.gateway.connect',
          message: translateNow('boot.steps.connectingGateway'),
          progress: 95
        })
        publish(conn)
        // Mint a fresh WS URL right before connecting. For OAuth gateways the
        // ticket is single-use with a short TTL, so the ticket baked into
        // conn.wsUrl is stale; resolveGatewayWsUrl() re-mints it and, on
        // failure, throws a reauth error rather than connecting with a dead
        // ticket (which would surface as an opaque "connection closed").
        const wsUrl = await resolveGatewayWsUrl(desktop, conn)
        await gateway.connect(wsUrl)

        if (cancelled) {
          return
        }

        // Record which profile the primary (window) backend booted as, so
        // same-profile resumes are no-op swaps and any reconnect targets the
        // right backend. Best-effort: a missing preference means "default".
        try {
          const pref = await desktop.profile?.get?.()
          const profileKey = (pref?.profile ?? '').trim() || 'default'
          $activeGatewayProfile.set(profileKey)
          setPrimaryGateway(gateway, profileKey)
          void ensureGatewayForProfile(profileKey)
        } catch {
          $activeGatewayProfile.set('default')
        }

        setDesktopBootStep({
          phase: 'renderer.config',
          message: translateNow('boot.steps.loadingSettings'),
          progress: 97
        })
        await ensureDefaultWorkspaceCwd()
        const remoteDefault = await desktopDefaultCwd().catch(() => null)
        if (remoteDefault?.cwd && !$activeSessionId.get() && !$currentCwd.get()) {
          setCurrentCwd(remoteDefault.cwd)
          setCurrentBranch(remoteDefault.branch || '')
        }
        await callbacksRef.current.refreshHermesConfig()

        if (cancelled) {
          return
        }

        setDesktopBootStep({
          phase: 'renderer.sessions',
          message: translateNow('boot.steps.loadingSessions'),
          progress: 99
        })
        await callbacksRef.current.refreshSessions()
        completeDesktopBoot()
        bootCompleted = true
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err)
          failDesktopBoot(message)
          notifyError(err, translateNow('boot.errors.desktopBootFailed'))
          setSessionsLoading(false)
        }
      }
    }

    void boot()

    return () => {
      cancelled = true
      clearReconnectTimer()
      clearInterval(keepaliveTimer)
      offWorking()
      offAttention()
      offActiveProfile()
      window.removeEventListener('online', onOnline)
      document.removeEventListener('visibilitychange', onVisible)
      offPowerResume?.()
      offState()
      offEvent()
      offExit()
      offWindowState?.()
      offBootProgress()
      closeSecondaryGateways()
      gateway.close()
      publish(null)
      callbacksRef.current.onGatewayReady(null)
      setPrimaryGateway(null)
      $gateway.set(null)
    }
  }, [])
}
