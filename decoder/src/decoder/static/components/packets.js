// Packets view: filter rail, packet list and the filtering itself.
import { html } from "../lib/preact.js";
import { direction, isUnknownFrame, matchesText } from "../lib/format.js";

export const EMPTY_FILTERS = {
    c2s: true,
    s2c: true,
    opcode: "",
    minSize: "",
    maxSize: "",
    bytes: "",
    unknownOnly: false,
};

// The frames that pass every active filter, in their original order.
export function filterFrames(frames, filters) {
    const min = filters.minSize ? Number(filters.minSize) : null;
    const max = filters.maxSize ? Number(filters.maxSize) : null;
    const bytesNeedle = filters.bytes.replace(/[^0-9a-fA-F]/g, "").toLowerCase();
    return frames.filter((frame) => {
        if (frame.direction === "C2S" && !filters.c2s) return false;
        if (frame.direction === "S2C" && !filters.s2c) return false;
        if (filters.unknownOnly && !isUnknownFrame(frame)) return false;
        if (!matchesText(filters.opcode, frame.opcode_hex, frame.name || "")) return false;
        if (min != null && frame.body_len < min) return false;
        if (max != null && frame.body_len > max) return false;
        if (bytesNeedle && !(frame.body_hex || "").toLowerCase().includes(bytesNeedle))
            return false;
        return true;
    });
}

function PacketRow({ frame, selected, onClick }) {
    const dir = direction(frame.direction);
    return html`<div
        class="prow ${selected ? "sel" : ""}"
        onClick=${onClick}
    >
        <span>#${frame._index}</span>
        <span class="t">${frame.t != null ? frame.t.toFixed(3) : ""}</span>
        <span class="op"><span class=${dir.cls}>${dir.arrow}</span> ${frame.opcode_hex}</span>
        <span class="nm ${isUnknownFrame(frame) ? "unk" : ""}"
            >${frame.name || "unknown"}${frame.error
                ? html`<span class="err-dot"> ✕</span>`
                : null}</span
        >
        <span class="sz">${frame.body_len}</span>
    </div>`;
}

export function PacketList({ frames, total, selectedIndex, onSelect }) {
    return html`<section class="list">
        <div class="list-head">
            <span class="title">Packets</span>
            <span class="count"
                >${frames.length.toLocaleString()} of ${total.toLocaleString()}</span
            >
        </div>
        <div class="prow colhdr">
            <span>#</span><span>t (s)</span><span>Opcode</span><span>Name</span
            ><span class="sz">Size</span>
        </div>
        <div class="list-body">
            ${frames.length
                ? frames.map(
                      (frame) => html`<${PacketRow}
                          key=${frame._index}
                          frame=${frame}
                          selected=${frame._index === selectedIndex}
                          onClick=${() => onSelect(frame._index)}
                      />`,
                  )
                : html`<div class="list-empty">No packets match the filters.</div>`}
        </div>
    </section>`;
}

export function FilterRail({ sessions, sessionIdx, setSessionIdx, filters, setFilters, onReset }) {
    const patch = (key) => (event) => {
        const target = event.target;
        const value = target.type === "checkbox" ? target.checked : target.value;
        setFilters((prev) => ({ ...prev, [key]: value }));
    };
    const session = sessions[sessionIdx] || {};

    return html`<aside class="rail">
        <div class="field">
            <label class="lbl" for="f-sess">Session</label>
            <select
                id="f-sess"
                class="in"
                value=${sessionIdx}
                onChange=${(e) => setSessionIdx(Number(e.target.value))}
            >
                ${sessions.map(
                    (s, i) => html`<option value=${i}>#${i + 1} · ${s.label}</option>`,
                )}
            </select>
            ${session.error
                ? html`<div class="sub text-err">${session.error}</div>`
                : null}
        </div>

        <div class="field">
            <span class="lbl">Direction</span>
            <label class="chk"
                ><input type="checkbox" checked=${filters.c2s} onChange=${patch("c2s")} /><span
                    class="dc"
                    >●</span
                >
                Client → Server</label
            >
            <label class="chk"
                ><input type="checkbox" checked=${filters.s2c} onChange=${patch("s2c")} /><span
                    class="ds"
                    >●</span
                >
                Server → Client</label
            >
        </div>

        <div class="field">
            <label class="lbl" for="f-op">Opcode / name</label>
            <input
                id="f-op"
                class="in"
                placeholder="0x0012, Move…"
                value=${filters.opcode}
                onInput=${patch("opcode")}
            />
        </div>

        <div class="field">
            <span class="lbl">Size (bytes)</span>
            <div class="size-row">
                <input
                    class="in"
                    inputmode="numeric"
                    placeholder="min"
                    value=${filters.minSize}
                    onInput=${patch("minSize")}
                />
                <span class="dash">–</span>
                <input
                    class="in"
                    inputmode="numeric"
                    placeholder="max"
                    value=${filters.maxSize}
                    onInput=${patch("maxSize")}
                />
            </div>
        </div>

        <div class="field">
            <label class="lbl" for="f-bytes">Search hex bytes</label>
            <input
                id="f-bytes"
                class="in"
                placeholder="3A 1F 00 00"
                value=${filters.bytes}
                onInput=${patch("bytes")}
            />
        </div>

        <label class="chk"
            ><input
                type="checkbox"
                checked=${filters.unknownOnly}
                onChange=${patch("unknownOnly")}
            />
            Unknown opcodes only</label
        >

        <span class="grow"></span>
        <button class="btn pri" onClick=${onReset}>Reset filters</button>
    </aside>`;
}
