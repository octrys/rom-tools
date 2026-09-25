// Entry point: capture loading, view state and layout.
import { html, render, Fragment, useState, useEffect, useMemo, useCallback } from "./lib/preact.js";
import { DetailPane } from "./components/detail.js";
import { Header } from "./components/header.js";
import { OpcodesView } from "./components/opcodes.js";
import { EMPTY_FILTERS, FilterRail, PacketList, filterFrames } from "./components/packets.js";

function App() {
    const [pcaps, setPcaps] = useState([]);
    const [activeName, setActiveName] = useState(null);
    const [data, setData] = useState(null);
    const [status, setStatus] = useState("idle"); // idle | loading | error | ready
    const [view, setView] = useState("packets");
    const [sessionIdx, setSessionIdx] = useState(0);
    const [selectedIndex, setSelectedIndex] = useState(null);
    const [filters, setFilters] = useState(EMPTY_FILTERS);

    const loadPcaps = useCallback(async () => {
        try {
            const res = await fetch("/api/pcaps");
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const body = await res.json();
            setPcaps(body.pcaps || []);
        } catch (err) {
            console.error("failed to list captures:", err);
            setPcaps([]);
        }
    }, []);

    useEffect(() => {
        loadPcaps();
    }, [loadPcaps]);

    const selectPcap = useCallback(async (name) => {
        if (!name) return;
        setActiveName(name);
        setStatus("loading");
        setData(null);
        setSessionIdx(0);
        setSelectedIndex(null);
        try {
            const res = await fetch(`/api/pcaps/${encodeURIComponent(name)}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            setData(await res.json());
            setStatus("ready");
            setPcaps((prev) =>
                prev.map((p) => (p.name === name ? { ...p, analyzed: true } : p)),
            );
        } catch (err) {
            console.error(`failed to analyze ${name}:`, err);
            setStatus("error");
        }
    }, []);

    const selectSession = useCallback((index) => {
        setSessionIdx(index);
        setSelectedIndex(null);
    }, []);

    const sessions = (data && data.sessions) || [];
    const session = sessions[sessionIdx] || null;

    const numberedFrames = useMemo(
        () => (session ? (session.frames || []).map((frame, i) => ({ ...frame, _index: i })) : []),
        [session],
    );

    const filtered = useMemo(
        () => filterFrames(numberedFrames, filters),
        [numberedFrames, filters],
    );

    const selectedPos = filtered.findIndex((f) => f._index === selectedIndex);
    const selectedFrame = selectedPos >= 0 ? filtered[selectedPos] : null;

    const resetFilters = useCallback(() => setFilters(EMPTY_FILTERS), []);

    // --- render states ---
    let main;
    if (status === "idle" || !activeName) {
        main = html`<div class="placeholder">
            Pick a capture in the top-right to begin.
        </div>`;
    } else if (status === "loading") {
        main = html`<div class="placeholder">Analyzing ${activeName}…</div>`;
    } else if (status === "error") {
        main = html`<div class="placeholder err">
            Failed to analyze ${activeName}.
        </div>`;
    } else if (view === "opcodes" && data) {
        main = session
            ? html`<${OpcodesView} key=${sessionIdx} session=${session} />`
            : html`<div class="placeholder">No packets in this session.</div>`;
    } else if (view === "packets" && data) {
        main = html`<${Fragment}>
            ${data.framing_matches === false &&
            html`<div class="warn">
                ⚠ detected framing differs from config — the protocol may have changed;
                re-confirm decoder.toml [framing].
            </div>`}
            <div class="body">
                <${FilterRail}
                    sessions=${sessions}
                    sessionIdx=${sessionIdx}
                    setSessionIdx=${selectSession}
                    filters=${filters}
                    setFilters=${setFilters}
                    onReset=${resetFilters}
                />
                <${PacketList}
                    frames=${filtered}
                    total=${numberedFrames.length}
                    selectedIndex=${selectedIndex}
                    onSelect=${setSelectedIndex}
                />
                ${selectedFrame
                    ? html`<${DetailPane}
                          frame=${selectedFrame}
                          index=${selectedFrame._index}
                          hasPrev=${selectedPos > 0}
                          hasNext=${selectedPos < filtered.length - 1}
                          onPrev=${() => setSelectedIndex(filtered[selectedPos - 1]._index)}
                          onNext=${() => setSelectedIndex(filtered[selectedPos + 1]._index)}
                      />`
                    : html`<section class="detail">
                          <div class="placeholder">
                              Select a packet to inspect its bytes and fields.
                          </div>
                      </section>`}
            </div>
        </${Fragment}>`;
    }

    return html`<div class="app">
        <${Header}
            view=${view}
            setView=${setView}
            pcaps=${pcaps}
            activeName=${activeName}
            onSelectPcap=${selectPcap}
            data=${data}
        />
        ${main}
    </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
