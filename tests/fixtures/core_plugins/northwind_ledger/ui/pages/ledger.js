// The ledger page of northwind-ledger: the project's entries through the bridge evo-plugin/1 (window.evo, the hub's
// UI kit 1), its words from i18n/<locale>.js, which the kit loads, the theme and the frame's height the kit's own.
(function () {
  "use strict";
  var evo = window.evo;
  evo.ready.then(function () {
    document.querySelectorAll("[data-i18n]").forEach(function (node) {
      node.textContent = evo.t(node.getAttribute("data-i18n"));
    });
    return evo.route("entries").then(function (answer) {
      var entries = answer.body.entries;
      var body = document.querySelector("#entries tbody");
      entries.forEach(function (entry) {
        var row = document.createElement("tr");
        [entry.memo, (entry.amount_cents / 100).toFixed(2), entry.due_on || ""].forEach(function (value, index) {
          var cell = document.createElement("td");
          if (index === 2) cell.className = "evo-hide-sm";
          cell.textContent = value;
          row.appendChild(cell);
        });
        body.appendChild(row);
      });
      document.getElementById("empty").hidden = entries.length > 0;
    });
  });
  document.getElementById("remind").addEventListener("click", function () {
    evo.route("remind", { method: "POST", body: {} }).then(function (answer) {
      evo.toast(evo.t("sent", { count: answer.body.sent }), "success");
    });
  });
})();
