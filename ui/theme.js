/* Quartermaster themes. Loaded in <head> so the choice applies before first paint.
   A theme is a stylesheet scoped to :root[data-theme="<id>"]; "plain" is style.css alone.
   The choice is kept per origin in localStorage (8790 and 8800 remember separately). */
(() => {
  const LIST = [["plain", "Plain"], ["muthur", "MU/TH/UR 6000"], ["castlevania", "Castlevania"], ["lcars", "LCARS"], ["cyberpunk", "Night City"],
    ["cortex", "Global Cortex"], ["concord", "Nerves Concord"],
    ["matrix", "The Matrix"], ["precrime", "Precrime"], ["ocp", "OCP"], ["magi", "MAGI"],
    ["hal", "HAL 9000"], ["gits", "Section 9"], ["wopr", "WOPR"],
    ["aperture", "Aperture Science"], ["jurassic", "Jurassic Park"],
    ["t800", "T-800"], ["codec", "Codec"], ["tron", "The Grid"],
    ["bladerunner", "Voight-Kampff"], ["goldeneye", "GoldenEye"], ["hexen", "Hexen"]];
  const KEY = "qm-theme";
  let t = "plain";
  try { t = localStorage.getItem(KEY) || "plain"; } catch (e) { /* private window: stay plain */ }
  const q = new URLSearchParams(location.search).get("theme");   // ?theme=muthur shows one without saving it
  if (q) t = q;
  if (!LIST.some((x) => x[0] === t)) t = "plain";
  document.documentElement.dataset.theme = t;
  window.QM_THEME = {
    list: LIST,
    get: () => document.documentElement.dataset.theme,
    set(v) {
      if (!LIST.some((x) => x[0] === v)) return;
      document.documentElement.dataset.theme = v;
      try { localStorage.setItem(KEY, v); } catch (e) { /* not remembered */ }
    },
  };
})();
