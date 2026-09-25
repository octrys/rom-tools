// Top bar: brand, view tabs, capture summary and capture picker.
import { html } from "../lib/preact.js";
import { Icon, Segmented } from "./common.js";

const VIEW_OPTIONS = [
    { value: "packets", label: "Packets" },
    { value: "opcodes", label: "Opcodes" },
];

function CaptureSummary({ activeName, data }) {
    if (!activeName) return html`<span>no capture selected</span>`;
    if (!data) return html`<span class="strong">${activeName}</span>`;
    const sessions = data.sessions || [];
    const packets = sessions.reduce((n, s) => n + (s.frames || []).length, 0);
    return html`<span class="strong">${activeName}</span><span>·</span
        ><span>${packets} packets</span><span>·</span
        ><span>${sessions.length} sessions</span>`;
}

export function Header({ view, setView, pcaps, activeName, onSelectPcap, data }) {
    return html`<header class="top">
        <div class="brand">
            <${Icon} path=${["M3 12h4l3-8 4 16 3-8h4"]} size=${22} />
            <span>packet<span class="slash">/</span>explorer</span>
        </div>
        <nav class="tabs" aria-label="Sections">
            <${Segmented}
                options=${VIEW_OPTIONS}
                value=${view}
                onChange=${setView}
                buttonClass="tab"
            />
        </nav>
        <span class="grow"></span>
        <div class="cap-info"><${CaptureSummary} activeName=${activeName} data=${data} /></div>
        <select
            class="in picker"
            value=${activeName || ""}
            onChange=${(e) => onSelectPcap(e.target.value)}
        >
            <option value="" disabled>Import / pick .pcap…</option>
            ${pcaps.map(
                (p) => html`<option value=${p.name}>${p.name}${p.analyzed ? " ●" : ""}</option>`,
            )}
        </select>
    </header>`;
}
