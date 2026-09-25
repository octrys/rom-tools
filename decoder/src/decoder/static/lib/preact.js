// Single place that pins the Preact / htm CDN versions.
import { h, render, Fragment } from "https://esm.sh/preact@10.24.3";
import {
    useState,
    useEffect,
    useMemo,
    useCallback,
} from "https://esm.sh/preact@10.24.3/hooks";
import htm from "https://esm.sh/htm@3.1.1";

export const html = htm.bind(h);
export { render, Fragment, useState, useEffect, useMemo, useCallback };
