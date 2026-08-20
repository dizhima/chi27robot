import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { backendUrl } from "./config";
import { uiTheme } from "./theme";

type TerminalEvent = {
  type: "output" | "status" | "error";
  data: string;
};

type TerminalPanelProps = {
  sceneContext?: string;
};

async function postTerminal(path: string, payload: Record<string, unknown> = {}) {
  const response = await fetch(`${backendUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`${path} failed: ${response.status}`);
  }
}

export function TerminalPanel({ sceneContext = "" }: TerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const eventsRef = useRef<EventSource | null>(null);
  const lastSceneContextRef = useRef("");
  const startedRef = useRef(false);
  const [started, setStarted] = useState(false);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    let resizeFrame = 0;
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "Cascadia Mono, Consolas, ui-monospace, monospace",
      fontSize: 13,
      lineHeight: 1.18,
      scrollback: 4000,
      theme: uiTheme === "light"
        ? {
            background: "#ffffff",
            foreground: "#26332d",
            cursor: "#2f7d57",
            selectionBackground: "#cfe5d9",
          }
        : {
            background: "#0b0f0e",
            foreground: "#d8e1dc",
            cursor: "#f4f7fb",
            selectionBackground: "#405048",
          },
    });
    const fit = new FitAddon();

    terminal.loadAddon(fit);
    terminal.open(containerRef.current);
    fit.fit();
    terminal.focus();
    terminalRef.current = terminal;
    fitRef.current = fit;

    terminal.onData((data) => {
      if (!startedRef.current) {
        return;
      }
      void postTerminal("/api/terminal/input", { data });
    });

    const resize = () => {
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        fit.fit();
        void postTerminal("/api/terminal/resize", {
          cols: terminal.cols,
          rows: terminal.rows,
        });
      });
    };

    const observer = new ResizeObserver(resize);
    observer.observe(containerRef.current);
    window.addEventListener("resize", resize);
    resize();

    return () => {
      eventsRef.current?.close();
      eventsRef.current = null;
      observer.disconnect();
      window.cancelAnimationFrame(resizeFrame);
      window.removeEventListener("resize", resize);
      terminal.dispose();
      terminalRef.current = null;
      fitRef.current = null;
    };
  }, []);

  useEffect(() => {
    startedRef.current = started;
    if (!started) {
      eventsRef.current?.close();
      eventsRef.current = null;
      setConnected(false);
      return;
    }

    const terminal = terminalRef.current;
    if (!terminal) {
      return;
    }

    const events = new EventSource(`${backendUrl}/api/terminal/events`);
    eventsRef.current = events;

    events.onopen = () => {
      setConnected(true);
    };

    events.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as TerminalEvent;
        if (event.type === "error") {
          terminal.write(`\r\n\x1b[31m${event.data}\x1b[0m`);
          return;
        }
        if (event.type === "status") {
          terminal.write(`\r\n\x1b[2m${event.data}\x1b[0m`);
          return;
        }
        terminal.write(event.data);
      } catch {
        terminal.write(message.data);
      }
    };

    events.onerror = () => {
      setConnected(false);
    };

    return () => {
      events.close();
      if (eventsRef.current === events) {
        eventsRef.current = null;
      }
    };
  }, [started]);

  useEffect(() => {
    const trimmedContext = sceneContext.trim();
    if (!started || !trimmedContext || trimmedContext === lastSceneContextRef.current) {
      return;
    }
    lastSceneContextRef.current = trimmedContext;
    terminalRef.current?.write(`\r\n\x1b[2m[scene] Sending active scene context to Codex CLI.\x1b[0m\r\n`);
    void postTerminal("/api/terminal/input", {
      data: `${trimmedContext}\r`,
    });
  }, [sceneContext, started]);

  const start = () => {
    terminalRef.current?.write("\r\n\x1b[2m[start] Connecting to Codex CLI.\x1b[0m\r\n");
    setStarted(true);
  };

  const restart = async () => {
    terminalRef.current?.clear();
    if (!started) {
      setStarted(true);
      return;
    }
    await postTerminal("/api/terminal/restart");
  };

  const stop = async () => {
    await postTerminal("/api/terminal/stop");
    setStarted(false);
  };

  return (
    <aside className="terminal-panel">
      <header className="terminal-header">
        <div>
          <strong>Codex CLI</strong>
          <span className={connected ? "terminal-dot is-connected" : "terminal-dot"} />
        </div>
        <div className="terminal-actions">
          <button type="button" onClick={start} disabled={started}>
            Start
          </button>
          <button type="button" onClick={restart}>
            Restart
          </button>
          <button type="button" onClick={stop} disabled={!started}>
            Stop
          </button>
        </div>
      </header>
      <div className="terminal-xterm-frame">
        <div ref={containerRef} className="terminal-xterm" />
      </div>
    </aside>
  );
}
