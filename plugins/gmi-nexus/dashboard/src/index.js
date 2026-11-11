/**
 * GMI NEXUS — Hermes dashboard tab.
 *
 * A plain IIFE, loaded by the dashboard as `dist/index.js`. No bundler, no
 * React import: the host exposes React and its utilities on
 * `window.__HERMES_PLUGIN_SDK__`, and the plugin draws with them.
 *
 * Two rules shape everything below.
 *
 * **Nothing is invented.** Every number on this screen came from the
 * plugin's backend, which reads the live GMI Cloud catalogue and the
 * recorded history. Where there is no data, the section says so and says
 * why. An estimate is labelled as an estimate; a measurement is labelled as
 * a measurement.
 *
 * **Nothing spends money.** The catalogue reads are free and the history is
 * already recorded. Generation is initiated by the agent, through the
 * plugin's tools, where Hermes' approval policy applies.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React) {
    console.warn("[gmi-nexus] the dashboard plugin SDK is unavailable; the tab is disabled.");
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var useCallback = SDK.hooks.useCallback;
  var fetchJSON = SDK.fetchJSON;

  var API = "/api/plugins/gmi-nexus";
  var PLUGIN = "gmi-nexus";

  // ----------------------------------------------------------------------- //
  // Formatting
  // ----------------------------------------------------------------------- //

  function usd(value) {
    var n = Number(value || 0);
    if (!isFinite(n)) return "—";
    if (n === 0) return "$0.00";
    if (n < 0.01) return "$" + n.toFixed(4);
    if (n < 1) return "$" + n.toFixed(3);
    return "$" + n.toFixed(2);
  }

  function count(value) {
    var n = Number(value || 0);
    return n.toLocaleString("en-US");
  }

  function ms(value) {
    var n = Number(value || 0);
    if (!n) return "—";
    if (n < 1000) return Math.round(n) + " ms";
    if (n < 60000) return (n / 1000).toFixed(1) + " s";
    return (n / 60000).toFixed(1) + " min";
  }

  function percent(value) {
    var n = Number(value || 0);
    return (n * 100).toFixed(n >= 0.995 || n === 0 ? 0 : 1) + "%";
  }

  function ago(ts) {
    if (!ts) return "—";
    var delta = (Date.now() / 1000 - Number(ts)) / 1;
    if (delta < 60) return "just now";
    if (delta < 3600) return Math.floor(delta / 60) + " min ago";
    if (delta < 86400) return Math.floor(delta / 3600) + " h ago";
    if (delta < 2592000) return Math.floor(delta / 86400) + " d ago";
    return new Date(Number(ts) * 1000).toISOString().slice(0, 10);
  }

  function shorten(text, max) {
    var value = String(text == null ? "" : text);
    if (value.length <= max) return value;
    return value.slice(0, max - 1) + "…";
  }

  /** The per-million-token price of a catalogue entry, in USD. */
  function perMillion(value) {
    var n = Number(value || 0);
    if (!n) return "—";
    return "$" + (n * 1e6).toFixed(2);
  }

  // ----------------------------------------------------------------------- //
  // Data access
  // ----------------------------------------------------------------------- //

  /**
   * Load a backend resource.
   *
   * The returned tuple is `[state, reload]`, where `state` is
   * `{loading, data, error}`. A failed load keeps the previous data on
   * screen and reports the failure — the panel never silently empties.
   */
  function useResource(path) {
    var pair = useState({ loading: true, data: null, error: null });
    var state = pair[0];
    var setState = pair[1];

    var load = useCallback(
      function () {
        setState(function (prev) {
          return { loading: true, data: prev.data, error: null };
        });
        fetchJSON(API + path)
          .then(function (data) {
            setState({ loading: false, data: data, error: null });
          })
          .catch(function (err) {
            setState({
              loading: false,
              data: null,
              error: String((err && err.message) || err || "the request failed"),
            });
          });
      },
      [path]
    );

    useEffect(load, [path]);
    return [state, load];
  }

  // ----------------------------------------------------------------------- //
  // Primitives
  // ----------------------------------------------------------------------- //

  function Panel(props) {
    return h(
      "section",
      { className: "nx-panel" + (props.className ? " " + props.className : "") },
      h(
        "header",
        { className: "nx-panel__head" },
        h("h2", { className: "nx-panel__title" }, props.title),
        props.action ? h("div", { className: "nx-panel__action" }, props.action) : null
      ),
      h("div", { className: "nx-panel__body" }, props.children)
    );
  }

  function Tile(props) {
    return h(
      "div",
      { className: "nx-tile" + (props.tone ? " nx-tile--" + props.tone : "") },
      h("span", { className: "nx-tile__label" }, props.label),
      h("span", { className: "nx-tile__value" }, props.value),
      props.hint ? h("span", { className: "nx-tile__hint" }, props.hint) : null
    );
  }

  function Pill(props) {
    return h(
      "span",
      { className: "nx-pill" + (props.tone ? " nx-pill--" + props.tone : "") },
      props.children
    );
  }

  function Empty(props) {
    return h(
      "div",
      { className: "nx-empty" },
      h("span", { className: "nx-empty__mark" }, "◦"),
      h("p", { className: "nx-empty__title" }, props.title),
      props.reason ? h("p", { className: "nx-empty__reason" }, props.reason) : null
    );
  }

  function Note(props) {
    return h("p", { className: "nx-note" }, props.children);
  }

  function Skeleton(props) {
    return h("div", { className: "nx-skeleton", style: { height: (props.height || 16) + "px" } });
  }

  function Loading(props) {
    return h(
      "div",
      { className: "nx-loading" },
      h(Skeleton, { height: 14 }),
      h(Skeleton, { height: 14 }),
      h(Skeleton, { height: 14 })
    );
  }

  function Table(props) {
    return h(
      "div",
      { className: "nx-tablewrap" },
      h(
        "table",
        { className: "nx-table" },
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            props.columns.map(function (column, index) {
              return h(
                "th",
                {
                  key: index,
                  className: column.align ? "nx-th nx-th--" + column.align : "nx-th",
                },
                column.label
              );
            })
          )
        ),
        h(
          "tbody",
          null,
          props.rows.map(function (row, rowIndex) {
            return h(
              "tr",
              { key: rowIndex, className: props.onRow ? "nx-tr nx-tr--click" : "nx-tr" },
              props.columns.map(function (column, cellIndex) {
                var content = column.render ? column.render(row, rowIndex) : row[column.key];
                return h(
                  "td",
                  {
                    key: cellIndex,
                    className: column.align ? "nx-td nx-td--" + column.align : "nx-td",
                  },
                  content === null || content === undefined ? "—" : content
                );
              })
            );
          })
        )
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Charts
  // ----------------------------------------------------------------------- //

  /** A horizontal bar list. Values are scaled against the largest one. */
  function BarList(props) {
    var items = props.items || [];
    var max = items.reduce(function (acc, item) {
      return Math.max(acc, Number(item.value || 0));
    }, 0);
    if (!items.length) {
      return h(Empty, { title: props.emptyTitle || "Nothing to show yet", reason: props.emptyReason });
    }
    return h(
      "ul",
      { className: "nx-bars" },
      items.map(function (item, index) {
        var value = Number(item.value || 0);
        var ratio = max > 0 ? value / max : 0;
        return h(
          "li",
          { key: index, className: "nx-bar" },
          h(
            "div",
            { className: "nx-bar__row" },
            h("span", { className: "nx-bar__label", title: item.label }, shorten(item.label, 42)),
            h("span", { className: "nx-bar__value" }, item.display || usd(value))
          ),
          h(
            "div",
            { className: "nx-bar__track" },
            h("div", {
              className: "nx-bar__fill" + (item.tone ? " nx-bar__fill--" + item.tone : ""),
              style: { width: Math.max(ratio * 100, value > 0 ? 2 : 0) + "%" },
            })
          )
        );
      })
    );
  }

  /** The spend series as an area chart. */
  function AreaChart(props) {
    var series = props.series || [];
    if (!series.length) {
      return h(Empty, {
        title: "No spend recorded in this window",
        reason: "A run has not been recorded yet, so there is nothing to plot.",
      });
    }
    var width = 640;
    var height = props.height || 180;
    var pad = { top: 12, right: 8, bottom: 24, left: 46 };
    var innerW = width - pad.left - pad.right;
    var innerH = height - pad.top - pad.bottom;
    var max = series.reduce(function (acc, point) {
      return Math.max(acc, Number(point.cost_usd || 0));
    }, 0);
    if (max <= 0) max = 1;

    var step = series.length > 1 ? innerW / (series.length - 1) : 0;
    var points = series.map(function (point, index) {
      var x = pad.left + (series.length > 1 ? index * step : innerW / 2);
      var y = pad.top + innerH - (Number(point.cost_usd || 0) / max) * innerH;
      return { x: x, y: y, point: point };
    });

    var line = points
      .map(function (p, index) {
        return (index === 0 ? "M" : "L") + p.x.toFixed(1) + " " + p.y.toFixed(1);
      })
      .join(" ");
    var area =
      points.length > 0
        ? line +
          " L" +
          points[points.length - 1].x.toFixed(1) +
          " " +
          (pad.top + innerH).toFixed(1) +
          " L" +
          points[0].x.toFixed(1) +
          " " +
          (pad.top + innerH).toFixed(1) +
          " Z"
        : "";

    var grid = [0, 0.25, 0.5, 0.75, 1].map(function (ratio) {
      var y = pad.top + innerH - ratio * innerH;
      return h("g", { key: ratio }, [
        h("line", {
          key: "l",
          x1: pad.left,
          x2: width - pad.right,
          y1: y,
          y2: y,
          className: "nx-chart__grid",
        }),
        h(
          "text",
          { key: "t", x: pad.left - 8, y: y + 4, className: "nx-axis", textAnchor: "end" },
          usd(max * ratio)
        ),
      ]);
    });

    var labels = [];
    var stride = Math.max(1, Math.ceil(series.length / 6));
    points.forEach(function (p, index) {
      if (index % stride !== 0 && index !== points.length - 1) return;
      labels.push(
        h(
          "text",
          {
            key: index,
            x: p.x,
            y: height - 6,
            className: "nx-axis",
            textAnchor: "middle",
          },
          String(p.point.day || "").slice(5)
        )
      );
    });

    var dots = points.map(function (p, index) {
      return h("circle", {
        key: index,
        cx: p.x,
        cy: p.y,
        r: 2.5,
        className: "nx-dot",
      });
    });

    return h(
      "div",
      { className: "nx-chart" },
      h(
        "svg",
        {
          viewBox: "0 0 " + width + " " + height,
          className: "nx-chart__svg",
          preserveAspectRatio: "none",
          role: "img",
          "aria-label": "Estimated spend over time",
        },
        h("defs", null, [
          h(
            "linearGradient",
            { key: "g", id: "nx-area-gradient", x1: "0", y1: "0", x2: "0", y2: "1" },
            [
              h("stop", { key: "a", offset: "0%", stopColor: "var(--nx-accent)", stopOpacity: "0.28" }),
              h("stop", { key: "b", offset: "100%", stopColor: "var(--nx-accent)", stopOpacity: "0" }),
            ]
          ),
        ]),
        grid,
        h("path", { d: area, fill: "url(#nx-area-gradient)", stroke: "none" }),
        h("path", { d: line, fill: "none", className: "nx-line" }),
        dots,
        labels
      )
    );
  }

  /** A donut of categorical shares. */
  function Donut(props) {
    var segments = props.segments || [];
    var total = segments.reduce(function (acc, segment) {
      return acc + Number(segment.value || 0);
    }, 0);
    if (!total) {
      return h(Empty, { title: "No models in this catalogue", reason: props.emptyReason });
    }
    var size = 148;
    var radius = 58;
    var stroke = 18;
    var circumference = 2 * Math.PI * radius;
    var offset = 0;
    var palette = ["--nx-accent", "--nx-accent-2", "--nx-accent-3", "--nx-accent-4"];

    var arcs = segments.map(function (segment, index) {
      var share = Number(segment.value || 0) / total;
      var length = share * circumference;
      var arc = h("circle", {
        key: segment.label,
        cx: size / 2,
        cy: size / 2,
        r: radius,
        fill: "none",
        stroke: "var(" + palette[index % palette.length] + ")",
        strokeWidth: stroke,
        strokeDasharray: length.toFixed(2) + " " + (circumference - length).toFixed(2),
        strokeDashoffset: (-offset).toFixed(2),
        transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")",
        strokeLinecap: "butt",
      });
      offset += length;
      return arc;
    });

    return h(
      "div",
      { className: "nx-donut" },
      h(
        "svg",
        { viewBox: "0 0 " + size + " " + size, className: "nx-donut__svg", role: "img" },
        h("circle", {
          cx: size / 2,
          cy: size / 2,
          r: radius,
          fill: "none",
          stroke: "var(--color-border)",
          strokeWidth: stroke,
        }),
        arcs
      ),
      h(
        "ul",
        { className: "nx-legend" },
        segments.map(function (segment, index) {
          return h(
            "li",
            { key: segment.label, className: "nx-legend__item" },
            h("span", {
              className: "nx-legend__swatch",
              style: { background: "var(" + palette[index % palette.length] + ")" },
            }),
            h("span", { className: "nx-legend__label" }, segment.label),
            h("span", { className: "nx-legend__value" }, count(segment.value))
          );
        })
      )
    );
  }

  /** A small inline trend line for a row of values. */
  function Sparkline(props) {
    var values = (props.values || []).map(Number);
    if (values.length < 2) return null;
    var max = Math.max.apply(null, values);
    var min = Math.min.apply(null, values);
    var span = max - min || 1;
    var width = 72;
    var height = 20;
    var path = values
      .map(function (value, index) {
        var x = (index / (values.length - 1)) * width;
        var y = height - ((value - min) / span) * (height - 3) - 1.5;
        return (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
      })
      .join(" ");
    return h(
      "svg",
      { viewBox: "0 0 " + width + " " + height, className: "nx-spark", "aria-hidden": "true" },
      h("path", { d: path, fill: "none", className: "nx-spark__path" })
    );
  }

  // ----------------------------------------------------------------------- //
  // Sections
  // ----------------------------------------------------------------------- //

  function OverviewSection() {
    var pair = useResource("/overview?days=30");
    var state = pair[0];
    var reload = pair[1];
    var data = state.data;

    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Overview" },
        h(Empty, {
          title: "The NEXUS backend did not answer",
          reason: state.error,
        }),
        h(
          "button",
          { className: "nx-btn", onClick: reload },
          "Try again"
        )
      );
    }
    if (!data) return h(Loading, null);

    var catalogue = data.catalogue || {};
    var budget = data.budget || {};
    var usage = data.usage || {};
    var runs = data.recent_runs || [];
    var stats = data.model_stats || [];
    var source = data.catalogue_source || {};

    var byModel = (usage.by_model || []).slice(0, 8).map(function (row) {
      return {
        label: row.model_id,
        value: Number(row.cost || 0),
        display: usd(row.cost),
      };
    });

    var successTotal = stats.reduce(function (acc, row) {
      return acc + Number(row.successes || 0);
    }, 0);
    var attemptTotal = stats.reduce(function (acc, row) {
      return acc + Number(row.runs || 0);
    }, 0);

    var budgetUsed = budget.daily_budget_usd
      ? Math.min(1, Number(budget.daily_spend_usd || 0) / Number(budget.daily_budget_usd))
      : 0;

    return h(
      "div",
      { className: "nx-stack" },
      h(
        "div",
        { className: "nx-tiles" },
        h(Tile, {
          label: "Language models",
          value: count(catalogue.llm_models),
          hint: "read live from GMI Cloud",
        }),
        h(Tile, {
          label: "Generation models",
          value: count(catalogue.media_models),
          hint: "image, video and audio",
        }),
        h(Tile, {
          label: "Vision-capable",
          value: count(catalogue.vision_capable),
          hint: "verified, not assumed",
        }),
        h(Tile, {
          label: "Runs recorded",
          value: count(runs.length),
          hint: stepSuccessHint(successTotal, attemptTotal),
        }),
        h(Tile, {
          label: "Estimated spend · 30 d",
          value: usd(usage.total_cost_usd),
          hint: count(usage.requests) + " requests",
          tone: "accent",
        })
      ),
      data.catalogue_error
        ? h(
            Panel,
            { title: "Catalogue" },
            h(Empty, {
              title: "The live catalogue could not be read",
              reason: data.catalogue_error,
            })
          )
        : null,
      h(
        "div",
        { className: "nx-grid nx-grid--2" },
        h(
          Panel,
          {
            title: "Spend over time",
            action: h(
              "span",
              { className: "nx-basis" },
              data.basis === "estimated" ? "estimated" : data.basis
            ),
          },
          h(AreaChart, { series: data.series || [] }),
          h(Note, null, data.spend_note)
        ),
        h(
          Panel,
          { title: "Budget" },
          h(
            "div",
            { className: "nx-budget" },
            h(
              "div",
              { className: "nx-budget__row" },
              h("span", { className: "nx-budget__label" }, "Spent today"),
              h(
                "span",
                { className: "nx-budget__value" },
                usd(budget.daily_spend_usd),
                h("span", { className: "nx-budget__of" }, " of " + usd(budget.daily_budget_usd))
              )
            ),
            h(
              "div",
              { className: "nx-track" },
              h("div", {
                className: "nx-track__fill",
                style: { width: Math.max(budgetUsed * 100, budgetUsed > 0 ? 2 : 0) + "%" },
              })
            ),
            h(
              "div",
              { className: "nx-budget__grid" },
              h(
                "div",
                null,
                h("span", { className: "nx-budget__label" }, "Remaining today"),
                h("span", { className: "nx-budget__value" }, usd(budget.daily_remaining_usd))
              ),
              h(
                "div",
                null,
                h("span", { className: "nx-budget__label" }, "Ceiling per run"),
                h("span", { className: "nx-budget__value" }, usd(budget.max_cost_per_run_usd))
              ),
              h(
                "div",
                null,
                h("span", { className: "nx-budget__label" }, "Approval above"),
                h("span", { className: "nx-budget__value" }, usd(budget.approval_threshold_usd))
              )
            ),
            h(Note, null, budget.note)
          )
        )
      ),
      h(
        "div",
        { className: "nx-grid nx-grid--2" },
        h(
          Panel,
          { title: "Spend by model" },
          h(BarList, {
            items: byModel,
            emptyTitle: "No spend recorded yet",
            emptyReason: "A run has not been recorded yet, so there is nothing to attribute.",
          })
        ),
        h(
          Panel,
          { title: "Catalogue provenance" },
          h(
            "dl",
            { className: "nx-defs" },
            def("Endpoint", source.endpoint || "—"),
            def("Media endpoint", source.media_endpoint || "—"),
            def("Fetched", source.fetched_at ? ago(source.fetched_at) : "never"),
            def("Age", source.age_seconds != null ? Math.round(source.age_seconds) + " s" : "—"),
            def("From cache", source.from_cache ? "yes" : "no"),
            def("Credential", data.credential_configured ? "configured" : "not configured")
          ),
          h(Note, null, source.note)
        )
      ),
      h(
        Panel,
        { title: "Recent runs" },
        runs.length
          ? h(Table, {
              columns: [
                { label: "Goal", render: function (row) {
                    return h("span", { className: "nx-strong", title: row.goal }, shorten(row.goal, 60));
                  } },
                { label: "Type", render: function (row) { return h(Pill, null, row.task_type); } },
                { label: "Status", render: function (row) {
                    return h(Pill, { tone: row.status === "completed" ? "good" : row.status === "failed" ? "bad" : "warn" }, row.status);
                  } },
                { label: "Models", render: function (row) { return shorten((row.models_used || []).join(", "), 34) || "—"; } },
                { label: "Cost", align: "right", render: function (row) { return usd(row.total_cost_usd); } },
                { label: "Latency", align: "right", render: function (row) { return ms(row.total_latency_ms); } },
                { label: "When", align: "right", render: function (row) { return ago(row.created_at); } },
              ],
              rows: runs,
            })
          : h(Empty, {
              title: "No run has been recorded yet",
              reason: data.empty_reason || "Run nexus_run to create one.",
            })
      )
    );
  }

  function successRate(successes, attempts) {
    if (!attempts) return "no measured steps";
    return Math.round((successes / attempts) * 100) + "%";
  }

  /** The tile hint under "Runs recorded" — a phrase that reads on its own. */
  function stepSuccessHint(successes, attempts) {
    if (!attempts) return "no measured steps yet";
    return Math.round((successes / attempts) * 100) + "% of steps succeeded";
  }

  function def(term, value) {
    return [
      h("dt", { key: "t" }, term),
      h("dd", { key: "d" }, value),
    ];
  }

  function ModelIntelligenceSection() {
    var pair = useResource("/models");
    var state = pair[0];
    var reload = pair[1];
    var filterPair = useState("");
    var filter = filterPair[0];
    var setFilter = filterPair[1];
    var selectedPair = useState(null);
    var selected = selectedPair[0];
    var setSelected = selectedPair[1];

    var data = state.data;

    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Model intelligence" },
        h(Empty, { title: "The NEXUS backend did not answer", reason: state.error })
      );
    }
    if (!data) return h(Loading, null);

    var models = data.models || [];
    var needle = filter.trim().toLowerCase();
    var visible = needle
      ? models.filter(function (model) {
          return (
            model.id.toLowerCase().indexOf(needle) >= 0 ||
            String(model.name || "").toLowerCase().indexOf(needle) >= 0 ||
            String(model.owned_by || "").toLowerCase().indexOf(needle) >= 0
          );
        })
      : models;

    var vision = models.filter(function (m) {
      return (m.capabilities || []).indexOf("vision") >= 0;
    }).length;
    var tools = models.filter(function (m) {
      return (m.capabilities || []).indexOf("tools") >= 0;
    }).length;
    var free = models.filter(function (m) {
      return m.is_free;
    }).length;

    var detail = null;
    if (selected) {
      for (var i = 0; i < models.length; i += 1) {
        if (models[i].id === selected) detail = models[i];
      }
    }

    return h(
      "div",
      { className: "nx-stack" },
      h(
        "div",
        { className: "nx-tiles" },
        h(Tile, { label: "Models in catalogue", value: count(models.length), hint: "live read" }),
        h(Tile, { label: "Vision-capable", value: count(vision), hint: "from catalogue or probe" }),
        h(Tile, { label: "Tool-capable", value: count(tools), hint: "can call tools" }),
        h(Tile, { label: "Free", value: count(free), hint: "priced at zero" })
      ),
      h(
        Panel,
        {
          title: "Catalogue",
          action: h(
            "div",
            { className: "nx-toolbar" },
            h("input", {
              className: "nx-input",
              type: "search",
              placeholder: "Filter by id, name or owner",
              value: filter,
              onChange: function (event) {
                setFilter(event.target.value);
              },
            }),
            h("button", { className: "nx-btn", onClick: reload }, "Refresh")
          ),
        },
        visible.length
          ? h(Table, {
              columns: [
                { label: "Model", render: function (row) {
                    return h(
                      "button",
                      {
                        className: "nx-link",
                        onClick: function () {
                          setSelected(row.id === selected ? null : row.id);
                        },
                      },
                      row.name || row.id
                    );
                  } },
                { label: "Owner", render: function (row) { return row.owned_by || "—"; } },
                { label: "Context", align: "right", render: function (row) {
                    return row.context_length ? count(row.context_length) : "—";
                  } },
                { label: "Capabilities", render: function (row) {
                    var caps = row.capabilities || [];
                    if (!caps.length) return h(Pill, { tone: "warn" }, "none published");
                    return h(
                      "span",
                      { className: "nx-pills" },
                      caps.map(function (cap, index) {
                        return h(Pill, { key: index, tone: cap === "vision" ? "accent" : null }, cap);
                      })
                    );
                  } },
                { label: "In / out", align: "right", render: function (row) {
                    var pricing = row.pricing || {};
                    return (
                      perMillion(pricing.prompt) + " / " + perMillion(pricing.completion)
                    );
                  } },
                { label: "Measured", align: "right", render: function (row) {
                    var measured = row.measured;
                    if (!measured) return h("span", { className: "nx-dim" }, "not measured");
                    return (
                      percent(Number(measured.successes || 0) / Math.max(1, Number(measured.runs || 0))) +
                      " · " +
                      ms(measured.ewma_latency_ms)
                    );
                  } },
              ],
              rows: visible,
            })
          : h(Empty, {
              title: needle ? "No model matches that filter" : "The catalogue is empty",
              reason: needle
                ? "Nothing in the live catalogue matches “" + filter + "”."
                : data.empty_reason || "The GMI Cloud catalogue could not be read.",
            })
      ),
      detail
        ? h(
            Panel,
            {
              title: detail.name || detail.id,
              action: h(
                "button",
                { className: "nx-btn", onClick: function () { setSelected(null); } },
                "Close"
              ),
            },
            h(
              "div",
              { className: "nx-grid nx-grid--2" },
              h(
                "dl",
                { className: "nx-defs" },
                def("Model id", detail.id),
                def("Owner", detail.owned_by || "—"),
                def("Context window", detail.context_length ? count(detail.context_length) : "—"),
                def("Ready", detail.is_ready ? "yes" : "no"),
                def("Free", detail.is_free ? "yes" : "no")
              ),
              h(
                "div",
                null,
                h("h3", { className: "nx-sub" }, "Capability evidence"),
                Object.keys(detail.capability_evidence || {}).length
                  ? h(
                      "ul",
                      { className: "nx-evidence" },
                      Object.keys(detail.capability_evidence).map(function (key) {
                        return h(
                          "li",
                          { key: key, className: "nx-evidence__item" },
                          h("span", { className: "nx-evidence__cap" }, key),
                          h("span", { className: "nx-evidence__text" }, detail.capability_evidence[key])
                        );
                      })
                    )
                  : h(Note, null, "No capability has been verified for this model yet.")
              )
            ),
            detail.description ? h(Note, null, detail.description) : null
          )
        : null
    );
  }

  function ArenaSection() {
    var pair = useResource("/arena");
    var state = pair[0];
    var reload = pair[1];
    var selectedPair = useState(null);
    var selected = selectedPair[0];
    var setSelected = selectedPair[1];

    var data = state.data;
    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Arena" },
        h(Empty, { title: "The NEXUS backend did not answer", reason: state.error })
      );
    }
    if (!data) return h(Loading, null);

    var benchmarks = data.benchmarks || [];
    var results = (selected && (data.results || {})[selected]) || [];
    var current = null;
    benchmarks.forEach(function (benchmark) {
      if (benchmark.id === selected) current = benchmark;
    });

    var best = {};
    results.forEach(function (row) {
      var score = Number(row.score == null ? 0 : row.score);
      if (!best[row.model_id] || score > best[row.model_id].score) {
        best[row.model_id] = { score: score, latency: row.latency_ms, cost: row.cost_usd };
      }
    });
    var leaderboard = Object.keys(best).map(function (id) {
      return { model: id, score: best[id].score, latency: best[id].latency, cost: best[id].cost };
    });
    leaderboard.sort(function (a, b) {
      return b.score - a.score || a.latency - b.latency;
    });

    return h(
      "div",
      { className: "nx-stack" },
      h(
        Panel,
        {
          title: "Benchmark runs",
          action: h("button", { className: "nx-btn", onClick: reload }, "Refresh"),
        },
        benchmarks.length
          ? h(Table, {
              columns: [
                { label: "Suite", render: function (row) { return h("span", { className: "nx-strong" }, row.suite); } },
                { label: "Models", render: function (row) { return shorten((row.models || []).join(", "), 50) || "—"; } },
                { label: "Status", render: function (row) {
                    return h(Pill, { tone: row.status === "completed" ? "good" : "warn" }, row.status);
                  } },
                { label: "When", align: "right", render: function (row) { return ago(row.created_at); } },
                { label: "", align: "right", render: function (row) {
                    return h(
                      "button",
                      {
                        className: "nx-link",
                        onClick: function () {
                          setSelected(row.id === selected ? null : row.id);
                        },
                      },
                      row.id === selected ? "Hide" : "Results"
                    );
                  } },
              ],
              rows: benchmarks,
            })
          : h(Empty, {
              title: "No benchmark has been run yet",
              reason: data.empty_reason,
            })
      ),
      current
        ? h(
            Panel,
            { title: "Leaderboard · " + current.suite },
            leaderboard.length
              ? h(Table, {
                  columns: [
                    { label: "#", align: "right", render: function (row, index) { return String(index + 1); } },
                    { label: "Model", render: function (row) { return h("span", { className: "nx-strong" }, row.model); } },
                    { label: "Score", align: "right", render: function (row) { return row.score.toFixed(3); } },
                    { label: "Latency", align: "right", render: function (row) { return ms(row.latency); } },
                    { label: "Cost", align: "right", render: function (row) { return usd(row.cost); } },
                  ],
                  rows: leaderboard,
                })
              : null,
            h("h3", { className: "nx-sub" }, "Every recorded result"),
            h(Table, {
              columns: [
                { label: "Model", render: function (row) { return row.model_id; } },
                { label: "Task", render: function (row) { return shorten(row.task, 40); } },
                { label: "Outcome", render: function (row) {
                    if (row.passed === null || row.passed === undefined) {
                      return h(Pill, { tone: "warn" }, "not scored");
                    }
                    return h(Pill, { tone: row.passed ? "good" : "bad" }, row.passed ? "passed" : "failed");
                  } },
                { label: "Score", align: "right", render: function (row) {
                    return row.score == null ? "—" : Number(row.score).toFixed(3);
                  } },
                { label: "Latency", align: "right", render: function (row) { return ms(row.latency_ms); } },
                { label: "Tokens", align: "right", render: function (row) {
                    return count(Number(row.prompt_tokens || 0) + Number(row.completion_tokens || 0));
                  } },
                { label: "Cost", align: "right", render: function (row) { return usd(row.cost_usd); } },
                { label: "Output", render: function (row) {
                    return row.error
                      ? h("span", { className: "nx-dim", title: row.error }, shorten(row.error, 46))
                      : h("span", { className: "nx-dim", title: row.output }, shorten(row.output, 46));
                  } },
              ],
              rows: results,
            }),
            h(Note, null, data.note)
          )
        : null
    );
  }

  function ExecutionSection() {
    var pair = useResource("/execution?days=30");
    var state = pair[0];
    var reload = pair[1];
    var selectedPair = useState(null);
    var selected = selectedPair[0];
    var setSelected = selectedPair[1];

    var data = state.data;
    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Execution" },
        h(Empty, { title: "The NEXUS backend did not answer", reason: state.error })
      );
    }
    if (!data) return h(Loading, null);

    var runs = data.runs || [];
    var stats = data.model_stats || [];
    var probes = data.capability_probes || [];

    return h(
      "div",
      { className: "nx-stack" },
      h(
        Panel,
        {
          title: "Runs",
          action: h("button", { className: "nx-btn", onClick: reload }, "Refresh"),
        },
        runs.length
          ? h(Table, {
              columns: [
                { label: "Goal", render: function (row) {
                    return h("span", { className: "nx-strong", title: row.goal }, shorten(row.goal, 56));
                  } },
                { label: "Type", render: function (row) { return h(Pill, null, row.task_type); } },
                { label: "Status", render: function (row) {
                    return h(Pill, { tone: row.status === "completed" ? "good" : row.status === "failed" ? "bad" : "warn" }, row.status);
                  } },
                { label: "Models", render: function (row) { return shorten((row.models_used || []).join(", "), 34) || "—"; } },
                { label: "Cost", align: "right", render: function (row) { return usd(row.total_cost_usd); } },
                { label: "Latency", align: "right", render: function (row) { return ms(row.total_latency_ms); } },
                { label: "When", align: "right", render: function (row) { return ago(row.created_at); } },
                { label: "", align: "right", render: function (row) {
                    return h(
                      "button",
                      {
                        className: "nx-link",
                        onClick: function () {
                          setSelected(row.id === selected ? null : row.id);
                        },
                      },
                      row.id === selected ? "Hide" : "Detail"
                    );
                  } },
              ],
              rows: runs,
            })
          : h(Empty, {
              title: "No run has been recorded yet",
              reason: data.empty_reason,
            })
      ),
      selected ? h(RunDetail, { runId: selected }) : null,
      h(
        Panel,
        { title: "Routing statistics" },
        stats.length
          ? h(Table, {
              columns: [
                { label: "Model", render: function (row) { return h("span", { className: "nx-strong" }, row.model_id); } },
                { label: "Task type", render: function (row) { return h(Pill, null, row.task_type); } },
                { label: "Runs", align: "right", render: function (row) { return count(row.runs); } },
                { label: "Success", align: "right", render: function (row) {
                    return percent(Number(row.successes || 0) / Math.max(1, Number(row.runs || 0)));
                  } },
                { label: "Failures", align: "right", render: function (row) { return count(row.failures); } },
                { label: "Latency (EWMA)", align: "right", render: function (row) { return ms(row.ewma_latency_ms); } },
                { label: "Cost (EWMA)", align: "right", render: function (row) { return usd(row.ewma_cost_usd); } },
                { label: "Last used", align: "right", render: function (row) { return row.last_used_at ? ago(row.last_used_at) : "—"; } },
              ],
              rows: stats,
            })
          : h(Empty, {
              title: "No routing statistics yet",
              reason: "They are recorded as runs are executed and verified.",
            })
      ),
      h(
        Panel,
        { title: "Verified capabilities" },
        probes.length
          ? h(Table, {
              columns: [
                { label: "Model", render: function (row) { return h("span", { className: "nx-strong" }, row.model_id); } },
                { label: "Capability", render: function (row) { return h(Pill, { tone: "accent" }, row.capability); } },
                { label: "Verified", render: function (row) {
                    return h(Pill, { tone: row.verified ? "good" : "bad" }, row.verified ? "yes" : "no");
                  } },
                { label: "Evidence", render: function (row) {
                    return h("span", { className: "nx-dim", title: row.evidence }, shorten(row.evidence, 70));
                  } },
                { label: "When", align: "right", render: function (row) { return ago(row.probed_at); } },
              ],
              rows: probes,
            })
          : h(Empty, {
              title: "No capability has been probed yet",
              reason: "NEXUS probes a model when it needs to be sure the model can do the work.",
            })
      )
    );
  }

  function RunDetail(props) {
    var pair = useResource("/execution?run_id=" + encodeURIComponent(props.runId));
    var state = pair[0];
    var data = state.data;
    var run = data && data.run ? data.run : null;

    var body;
    if (state.loading && !run) {
      body = h(Loading, null);
    } else if (state.error) {
      body = h(Empty, { title: "The run could not be loaded", reason: state.error });
    } else if (!run) {
      body = h(Empty, { title: "No run with that id is recorded" });
    } else {
      var report = null;
      try {
        report = run.report ? JSON.parse(run.report) : null;
      } catch (err) {
        report = null;
      }
      var steps = (report && report.steps) || [];
      var routes = (report && report.routes) || [];
      body = h(
        "div",
        { className: "nx-stack" },
        h(
          "dl",
          { className: "nx-defs" },
          def("Goal", run.goal),
          def("Status", run.status),
          def("Task type", run.task_type),
          def("Models", (run.models_used || []).join(", ") || "—"),
          def("Estimated cost", usd(run.total_cost_usd)),
          def("Total latency", ms(run.total_latency_ms))
        ),
        h(
          "h3",
          { className: "nx-sub" },
          "Steps"
        ),
        steps.length
          ? h(Table, {
              columns: [
                { label: "#", align: "right", render: function (row, index) { return String(index + 1); } },
                { label: "Description", render: function (row) {
                    return h("span", { className: "nx-strong", title: row.description }, shorten(row.description, 54));
                  } },
                { label: "Type", render: function (row) { return h(Pill, null, row.task_type); } },
                { label: "Model", render: function (row) { return row.model_id || "—"; } },
                { label: "Status", render: function (row) {
                    return h(Pill, { tone: stepTone(row.status) }, row.status);
                  } },
                { label: "Attempts", align: "right", render: function (row) { return count(row.attempts); } },
                { label: "Latency", align: "right", render: function (row) { return ms(row.latency_ms); } },
                { label: "Cost", align: "right", render: function (row) {
                    var usage = row.usage || {};
                    return usd(usage.cost_usd);
                  } },
              ],
              rows: steps,
            })
          : h(Note, null, "The stored report carries no step detail."),
        routes.length
          ? h(
              "div",
              null,
              h("h3", { className: "nx-sub" }, "Routing decisions"),
              routes.map(function (route, index) {
                var candidates = route.candidates || [];
                // ``rejected`` is a map of model id to the reason it was ruled
                // out, not a list.
                var rejected = route.rejected || {};
                var rejectedEntries = Object.keys(rejected).map(function (modelId) {
                  return { model: modelId, reason: rejected[modelId] };
                });
                return h(
                  "div",
                  { key: index, className: "nx-route" },
                  h(
                    "div",
                    { className: "nx-route__head" },
                    h("span", { className: "nx-route__type" }, route.task_type || "step"),
                    h("span", { className: "nx-route__strategy" }, route.strategy || "")
                  ),
                  candidates.length
                    ? h(
                        "ul",
                        { className: "nx-candidates" },
                        candidates.map(function (candidate, candidateIndex) {
                          return h(
                            "li",
                            { key: candidateIndex, className: "nx-candidate" },
                            h("span", { className: "nx-candidate__model" },
                              (candidate.model && (candidate.model.id || candidate.model)) || "—"),
                            h("span", { className: "nx-candidate__score" },
                              candidate.score != null ? Number(candidate.score).toFixed(3) : "—"),
                            h("span", { className: "nx-candidate__reason" },
                              shorten(reasonText(candidate.reasons || candidate.reason), 70))
                          );
                        })
                      )
                    : null,
                  rejectedEntries.length
                    ? h(
                        "ul",
                        { className: "nx-rejected" },
                        rejectedEntries.map(function (entry, rejectedIndex) {
                          return h(
                            "li",
                            { key: rejectedIndex, className: "nx-rejected__item" },
                            h("span", { className: "nx-rejected__model" }, entry.model),
                            h("span", { className: "nx-rejected__reason" }, entry.reason)
                          );
                        })
                      )
                    : null
                );
              })
            )
          : null
      );
    }

    return h(
      Panel,
      {
        title: "Run " + props.runId,
        action: h(
          "span",
          { className: "nx-basis" },
          reportBasis(run)
        ),
      },
      body
    );
  }

  function reportBasis(run) {
    if (!run) return "";
    return run.total_cost_usd ? "estimated cost" : "no spend";
  }

  /** The tone for a step outcome: ok, failed or skipped. */
  function stepTone(status) {
    if (status === "ok" || status === "completed") return "good";
    if (status === "failed" || status === "error") return "bad";
    return "warn";
  }

  /** The router's reasons arrive as a list; flatten them for display. */
  function reasonText(reasons) {
    if (Array.isArray(reasons)) return reasons.join("; ");
    return String(reasons == null ? "" : reasons);
  }

  function MultimodalSection() {
    var pair = useResource("/multimodal");
    var state = pair[0];
    var reload = pair[1];
    var data = state.data;

    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Multimodal" },
        h(Empty, { title: "The NEXUS backend did not answer", reason: state.error })
      );
    }
    if (!data) return h(Loading, null);

    var models = data.models || [];
    var requests = data.requests || [];
    var segments = Object.keys(data.by_modality || {}).map(function (key) {
      return { label: key, value: data.by_modality[key] };
    });

    return h(
      "div",
      { className: "nx-stack" },
      h(
        "div",
        { className: "nx-grid nx-grid--2" },
        h(
          Panel,
          { title: "Catalogue by modality" },
          h(Donut, {
            segments: segments,
            emptyReason: "The generation catalogue could not be read.",
          }),
          data.truncated
            ? h(Note, null, "Showing " + data.described + " of " + data.count + " models with their published schema.")
            : null
        ),
        h(
          Panel,
          { title: "Recent generations" },
          requests.length
            ? h(Table, {
                columns: [
                  { label: "Model", render: function (row) { return h("span", { className: "nx-strong" }, row.model_id); } },
                  { label: "Status", render: function (row) {
                      return h(Pill, { tone: row.status === "success" ? "good" : row.status === "failed" ? "bad" : "warn" }, row.status);
                    } },
                  { label: "Assets", align: "right", render: function (row) { return count((row.media_urls || []).length); } },
                  { label: "Est. cost", align: "right", render: function (row) { return usd(row.cost_usd); } },
                  { label: "When", align: "right", render: function (row) { return ago(row.created_at); } },
                ],
                rows: requests,
              })
            : h(Empty, {
                title: "No generation has been recorded yet",
                reason: "Generations are submitted by the agent through nexus_media.",
              })
        )
      ),
      h(
        Panel,
        {
          title: "Generation models",
          action: h("button", { className: "nx-btn", onClick: reload }, "Refresh"),
        },
        models.length
          ? h(
              "div",
              { className: "nx-mediacards" },
              models.map(function (model) {
                var spec = model.spec || {};
                return h(
                  "article",
                  { key: model.id, className: "nx-mediacard" },
                  h(
                    "header",
                    { className: "nx-mediacard__head" },
                    h("span", { className: "nx-mediacard__name" }, spec.display_name || model.id),
                    h(Pill, { tone: "accent" }, model.modality)
                  ),
                  spec.provider ? h("p", { className: "nx-mediacard__meta" }, spec.provider) : null,
                  model.description
                    ? h("p", { className: "nx-mediacard__desc" }, shorten(model.description, 140))
                    : null,
                  h(
                    "dl",
                    { className: "nx-defs nx-defs--tight" },
                    def("Unit", spec.unit || "—"),
                    def("Unit price", spec.unit_price_usd != null ? usd(spec.unit_price_usd) : "—"),
                    def("Discount", spec.discount ? percent(spec.discount) : "none"),
                    def("Status", spec.status || "—")
                  ),
                  spec.parameters && spec.parameters.length
                    ? h(
                        "div",
                        null,
                        h("h4", { className: "nx-sub nx-sub--small" }, "Published parameters"),
                        h(
                          "ul",
                          { className: "nx-params" },
                          spec.parameters.map(function (parameter, index) {
                            return h(
                              "li",
                              { key: index, className: "nx-param" },
                              h("span", { className: "nx-param__name" }, parameter.name),
                              parameter.required
                                ? h(Pill, { tone: "warn" }, "required")
                                : null,
                              h("span", { className: "nx-param__type" }, parameter.type),
                              parameter.options && parameter.options.length
                                ? h(
                                    "span",
                                    { className: "nx-param__options" },
                                    shorten(parameter.options.join(", "), 46)
                                  )
                                : null
                            );
                          })
                        )
                      )
                    : h(Note, null, "This model published no parameter schema, so NEXUS will not build a request for it.")
                );
              })
            )
          : h(Empty, {
              title: "The generation catalogue could not be read",
              reason: data.empty_reason,
            })
      ),
      h(Note, null, data.note)
    );
  }

  function SettingsSection() {
    var pair = useResource("/settings");
    var state = pair[0];
    var reload = pair[1];
    var data = state.data;

    if (state.loading && !data) return h(Loading, null);
    if (state.error) {
      return h(
        Panel,
        { title: "Settings" },
        h(Empty, { title: "The NEXUS backend did not answer", reason: state.error })
      );
    }
    if (!data) return h(Loading, null);

    var settings = data.settings || {};
    var budget = data.budget || {};
    var labels = {
      llm_base_url: "Inference base URL",
      media_base_url: "Generation base URL",
      mcp_server: "MCP server",
      use_mcp: "Prefer the MCP path",
      request_timeout_s: "Request timeout (s)",
      max_retries: "Retries per step",
      max_parallel: "Parallel steps",
      max_cost_per_run_usd: "Cost ceiling per run",
      daily_budget_usd: "Daily budget",
      approval_threshold_usd: "Approval threshold",
      preferred_models: "Preferred models",
      blocked_models: "Blocked models",
      verify_vision_capability: "Verify vision capability",
      enable_learning: "Adaptive learning",
      retention_days: "History retention (days)",
    };

    var rows = Object.keys(settings).map(function (key) {
      var value = settings[key];
      if (Array.isArray(value)) value = value.length ? value.join(", ") : "none";
      if (typeof value === "boolean") value = value ? "on" : "off";
      return { label: labels[key] || key, value: String(value) };
    });

    return h(
      "div",
      { className: "nx-stack" },
      h(
        Panel,
        {
          title: "Configuration in force",
          action: h("button", { className: "nx-btn", onClick: reload }, "Refresh"),
        },
        h(Table, {
          columns: [
            { label: "Setting", render: function (row) { return h("span", { className: "nx-strong" }, row.label); } },
            { label: "Value", render: function (row) { return h("span", { className: "nx-mono" }, row.value); } },
          ],
          rows: rows,
        }),
        h(Note, null, data.note)
      ),
      h(
        "div",
        { className: "nx-grid nx-grid--2" },
        h(
          Panel,
          { title: "Credential" },
          h(
            "div",
            { className: "nx-credential" },
            h(
              "span",
              { className: "nx-credential__dot" + (data.credential_configured ? " nx-credential__dot--on" : "") }
            ),
            h(
              "span",
              { className: "nx-credential__label" },
              data.credential_configured ? "Configured" : "Not configured"
            )
          ),
          h(Note, null, data.credential_note)
        ),
        h(
          Panel,
          { title: "Spend limits" },
          h(
            "dl",
            { className: "nx-defs" },
            def("Daily budget", usd(budget.daily_budget_usd)),
            def("Spent today", usd(budget.daily_spend_usd)),
            def("Remaining today", usd(budget.daily_remaining_usd)),
            def("Ceiling per run", usd(budget.max_cost_per_run_usd)),
            def("Approval above", usd(budget.approval_threshold_usd))
          ),
          h(Note, null, budget.note)
        )
      )
    );
  }

  // ----------------------------------------------------------------------- //
  // Shell
  // ----------------------------------------------------------------------- //

  var SECTIONS = [
    { id: "overview", label: "Overview", hint: "Catalogue, budget and recent runs", render: OverviewSection },
    { id: "models", label: "Model Intelligence", hint: "The live catalogue and what was measured", render: ModelIntelligenceSection },
    { id: "arena", label: "Arena", hint: "Real side-by-side measurements", render: ArenaSection },
    { id: "execution", label: "Execution", hint: "Runs, steps and routing decisions", render: ExecutionSection },
    { id: "multimodal", label: "Multimodal", hint: "Image, video and audio models", render: MultimodalSection },
    { id: "settings", label: "Settings", hint: "Configuration and spend limits", render: SettingsSection },
  ];

  function NexusDashboard() {
    var activePair = useState("overview");
    var active = activePair[0];
    var setActive = activePair[1];

    var section = useMemo(function () {
      for (var i = 0; i < SECTIONS.length; i += 1) {
        if (SECTIONS[i].id === active) return SECTIONS[i];
      }
      return SECTIONS[0];
    }, [active]);

    var Body = section.render;

    return h(
      "div",
      { className: "nx-root" },
      h(
        "header",
        { className: "nx-topbar" },
        h(
          "div",
          { className: "nx-topbar__identity" },
          h("span", { className: "nx-mark" }, "◈"),
          h(
            "div",
            null,
            h("h1", { className: "nx-title" }, "GMI NEXUS"),
            h("p", { className: "nx-subtitle" }, "One agent. Many models. One intelligent system.")
          )
        ),
        h(
          "div",
          { className: "nx-topbar__meta" },
          h("span", { className: "nx-chip" }, "GMI Cloud"),
          h("span", { className: "nx-chip nx-chip--quiet" }, "Reads only — this view never spends credits")
        )
      ),
      h(
        "div",
        { className: "nx-layout" },
        h(
          "nav",
          { className: "nx-nav" },
          h(
            "ul",
            null,
            SECTIONS.map(function (item) {
              return h(
                "li",
                { key: item.id },
                h(
                  "button",
                  {
                    className: "nx-nav__item" + (item.id === active ? " nx-nav__item--active" : ""),
                    onClick: function () {
                      setActive(item.id);
                    },
                  },
                  h("span", { className: "nx-nav__label" }, item.label),
                  h("span", { className: "nx-nav__hint" }, item.hint)
                )
              );
            })
          )
        ),
        h(
          "main",
          { className: "nx-main" },
          h(
            "div",
            { className: "nx-main__head" },
            h("h2", { className: "nx-main__title" }, section.label),
            h("p", { className: "nx-main__hint" }, section.hint)
          ),
          h(Body, null)
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN, NexusDashboard);
})();
