// Hex viewer with byte selection, and the interpreter for the selected bytes.
import { html } from "../lib/preact.js";
import { toHex } from "../lib/format.js";
import { Segmented } from "./common.js";

const HEX_COLUMNS = Array.from({ length: 16 }, (_, i) => toHex(i, 2));

const ENDIAN_OPTIONS = [
    { value: true, label: "LE" },
    { value: false, label: "BE" },
];

function asciiOf(bytes) {
    return bytes.map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : "·")).join("");
}

export function HexView({ bytes, bodyLen, selected, onSelect }) {
    if (!bytes.length) return html`<div class="hex-empty">(empty body)</div>`;
    const rows = [];
    for (let off = 0; off < bytes.length; off += 16)
        rows.push({ off, slice: Array.from(bytes.slice(off, off + 16)) });

    return html`<div class="hexcard">
        <div class="hex-colhdr">
            <span class="off">offset</span>
            <span class="cols"
                >${HEX_COLUMNS.map((c) => html`<span class="hexb">${c}</span>`)}</span
            >
            <span>ascii</span>
        </div>
        ${rows.map(
            (row) => html`<div class="hex-row">
                <span class="off">${toHex(row.off, 4)}</span>
                <span class="cols">
                    ${row.slice.map((b, i) => {
                        const off = row.off + i;
                        const cls = `hexb byte${b === 0 ? " z" : ""}${
                            off === selected ? " sel" : ""
                        }`;
                        return html`<span class=${cls} onClick=${() => onSelect(off)}
                            >${toHex(b, 2)}</span
                        >`;
                    })}
                </span>
                <span class="ascii">${asciiOf(row.slice)}</span>
            </div>`,
        )}
        ${bodyLen > bytes.length &&
        html`<div class="hex-more">… ${bodyLen - bytes.length} more bytes (truncated)</div>`}
    </div>`;
}

function readInterp(bytes, offset, little) {
    if (offset == null || offset >= bytes.length) return [];
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const out = [{ k: "uint8", v: String(view.getUint8(offset)) }];
    if (offset + 2 <= bytes.length)
        out.push({ k: "uint16", v: String(view.getUint16(offset, little)) });
    if (offset + 4 <= bytes.length) {
        out.push({ k: "int32", v: String(view.getInt32(offset, little)) });
        out.push({
            k: "float32",
            v: Number(view.getFloat32(offset, little).toPrecision(7)).toString(),
        });
    }
    return out;
}

export function Interpreter({ bytes, selected, little, setLittle }) {
    if (selected == null)
        return html`<div class="interp">
            <div class="sel-info">
                <span class="lbl">Selection</span>
                <span class="sel-val muted">click a byte in the hex view</span>
            </div>
        </div>`;

    const end = Math.min(selected + 4, bytes.length);
    const rangeBytes = Array.from(bytes.slice(selected, end))
        .map((b) => toHex(b, 2))
        .join(" ");
    const last = end - 1;
    const label = `0x${toHex(selected, 2)}${last > selected ? `–0x${toHex(last, 2)}` : ""}`;

    return html`<div class="interp">
        <div class="sel-info">
            <span class="lbl">Selection</span>
            <span class="sel-val">${label} · ${rangeBytes}</span>
        </div>
        ${readInterp(bytes, selected, little).map(
            (item) =>
                html`<div class="ip">
                    <span class="ipk">${item.k}</span>
                    <span class="ipv">${item.v}</span>
                </div>`,
        )}
        <div class="endian">
            <${Segmented} options=${ENDIAN_OPTIONS} value=${little} onChange=${setLittle} />
        </div>
    </div>`;
}
