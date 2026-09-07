import { mount } from "svelte";
import App from "./App.svelte";
import { reloadIfStale } from "./lib/freshness";
import "./app.css";

// A browser holding a superseded bundle looks like a broken app, so this
// checks and reloads once before anything else runs.
void reloadIfStale();

const target = document.getElementById("app");
if (!target) throw new Error("#app is missing from the page");

export default mount(App, { target });
