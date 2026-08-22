/* The hero's four scenes, cycling.

   A straight port of the design's own component logic, minus the design tool's runtime:
   the four scenes are already in the document and this only toggles which one is shown,
   retitles the badge above them, and marks the current tab.

   The two intervals are the design's and are deliberate — it advances every 4.5s on its
   own, and slows to 7s once you have picked a scene yourself, because someone who just
   clicked is reading rather than watching. */

(function () {
  "use strict";

  var scenes = [].slice.call(document.querySelectorAll(".scene"));
  var tabs = [].slice.call(document.querySelectorAll("#scene-tabs button"));
  var badge = document.getElementById("run-badge");
  if (!scenes.length || !badge) return;

  // What the run itself is doing while each scene is on screen.
  var BADGES = [
    ["b-dim", "draft"],
    ["b-acc pulse", "running"],
    ["b-warn pulse", "waiting on human"],
    ["b-warn pulse", "waiting on human"],
  ];

  var at = 0;
  var timer = null;

  function show(i) {
    at = i;
    scenes.forEach(function (scene, n) {
      scene.style.display = n === i ? "block" : "none";
    });
    badge.className = "badge " + BADGES[i][0];
    badge.textContent = BADGES[i][1];
    tabs.forEach(function (tab, n) {
      var on = n === i;
      tab.style.color = on ? "var(--accent-ink)" : "var(--muted)";
      tab.style.borderBottomColor = on ? "var(--accent)" : "transparent";
      tab.setAttribute("aria-current", on ? "true" : "false");
    });
  }

  function cycle(every) {
    clearInterval(timer);
    timer = setInterval(function () { show((at + 1) % scenes.length); }, every);
  }

  tabs.forEach(function (tab, n) {
    tab.addEventListener("click", function () {
      show(n);
      // Only re-arm if it was moving to begin with: someone who asked not to see motion
      // still gets working tabs, they just do not start advancing because of a click.
      if (timer) cycle(7000);
    });
  });

  show(0);
  if (!window.matchMedia || !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    cycle(4500);
  }
})();
