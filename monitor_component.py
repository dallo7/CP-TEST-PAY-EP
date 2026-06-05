from dash import Input, Output, State, callback_context, dcc, html


DARK = "#0d0f14"
SURF = "#161a22"
BORD = "#252b38"
ACC2 = "#f07a3a"
TEXT = "#e8eaf0"
MUTED = "#6b7280"
GREEN = "#22c55e"
RED = "#ef4444"
WARN = "#f59e0b"


def _rgba(hex_color, alpha):
    r, g, b = (int(hex_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def stat_card_m(label, value_id, color=TEXT):
    return html.Div(
        style={
            "background": SURF,
            "border": f"1px solid {BORD}",
            "borderRadius": "13px",
            "padding": "16px 18px",
            "minWidth": "120px",
        },
        children=[
            html.Div(
                label,
                style={
                    "fontSize": "10px",
                    "color": MUTED,
                    "fontWeight": "600",
                    "textTransform": "uppercase",
                    "letterSpacing": ".07em",
                    "marginBottom": "5px",
                },
            ),
            html.Div(
                "-",
                id=value_id,
                style={
                    "fontSize": "24px",
                    "fontWeight": "700",
                    "color": color,
                    "lineHeight": "1",
                },
            ),
        ],
    )


def badge_m(text, color=MUTED):
    return html.Span(
        text,
        style={
            "background": _rgba(color, 0.15),
            "border": f"1px solid {_rgba(color, 0.3)}",
            "color": color,
            "borderRadius": "20px",
            "padding": "3px 10px",
            "fontSize": "11px",
            "fontWeight": "700",
            "textTransform": "uppercase",
            "letterSpacing": ".04em",
            "marginRight": "6px",
        },
    )


def kv_item_m(label, value):
    return html.Div(
        style={"padding": "4px 0"},
        children=[
            html.Dt(
                label,
                style={
                    "fontSize": "10px",
                    "color": MUTED,
                    "fontWeight": "600",
                    "textTransform": "uppercase",
                    "letterSpacing": ".06em",
                },
            ),
            html.Dd(
                str(value) if value not in (None, "") else "-",
                style={
                    "fontSize": "12px",
                    "color": TEXT,
                    "fontFamily": "monospace",
                    "marginTop": "1px",
                },
            ),
        ],
    )


def monitor_layout():
    return html.Div(
        [
            html.Div(
                style={
                    "display": "flex",
                    "gap": "12px",
                    "flexWrap": "wrap",
                    "marginBottom": "20px",
                },
                children=[
                    stat_card_m("Total received", "s-total"),
                    stat_card_m("Settled", "s-settled", GREEN),
                    stat_card_m("Hash mismatches", "s-bad", RED),
                    stat_card_m("Total paid (TZS)", "s-amount"),
                ],
            ),
            html.Div(
                style={
                    "display": "flex",
                    "gap": "10px",
                    "flexWrap": "wrap",
                    "alignItems": "center",
                    "background": SURF,
                    "border": f"1px solid {BORD}",
                    "borderRadius": "11px",
                    "padding": "12px 16px",
                    "marginBottom": "16px",
                },
                children=[
                    dcc.Input(
                        id="m-search",
                        type="text",
                        debounce=True,
                        placeholder="Search invoice, ref, phone...",
                        style={
                            "background": "#1e2330",
                            "border": f"1px solid {BORD}",
                            "borderRadius": "8px",
                            "color": TEXT,
                            "padding": "6px 12px",
                            "fontSize": "12px",
                            "flex": "1",
                            "minWidth": "180px",
                            "outline": "none",
                        },
                    ),
                    dcc.Dropdown(
                        id="m-status",
                        value="",
                        clearable=False,
                        options=[
                            {"label": "All statuses", "value": ""},
                            {"label": "Settled", "value": "settled"},
                            {"label": "Pending", "value": "pending"},
                            {"label": "Failed", "value": "failed"},
                        ],
                        style={"width": "160px", "fontSize": "12px"},
                    ),
                    dcc.Dropdown(
                        id="m-hash",
                        value="",
                        clearable=False,
                        options=[
                            {"label": "All", "value": ""},
                            {"label": "Hash valid", "value": "valid"},
                            {"label": "Hash invalid", "value": "invalid"},
                        ],
                        style={"width": "150px", "fontSize": "12px"},
                    ),
                    html.Button(
                        "Inject test event",
                        id="btn-test-event",
                        n_clicks=0,
                        style={
                            "background": _rgba(ACC2, 0.12),
                            "border": f"1px solid {_rgba(ACC2, 0.3)}",
                            "color": ACC2,
                            "borderRadius": "8px",
                            "padding": "6px 14px",
                            "fontSize": "12px",
                            "cursor": "pointer",
                        },
                    ),
                    html.Button(
                        "Clear all",
                        id="btn-clear-notif",
                        n_clicks=0,
                        style={
                            "background": _rgba(RED, 0.12),
                            "border": f"1px solid {_rgba(RED, 0.3)}",
                            "color": RED,
                            "borderRadius": "8px",
                            "padding": "6px 14px",
                            "fontSize": "12px",
                            "cursor": "pointer",
                        },
                    ),
                ],
            ),
            html.Div(id="m-feed"),
            html.Div(
                style={"marginTop": "36px"},
                children=[
                    html.Div(
                        style={
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "12px",
                            "marginBottom": "12px",
                        },
                        children=[
                            html.H2(
                                "CapitalPay Server IPs",
                                style={
                                    "fontFamily": "DM Serif Display,serif",
                                    "fontSize": "17px",
                                    "color": TEXT,
                                },
                            ),
                            html.Span(id="ip-count", style={"fontSize": "12px", "color": MUTED}),
                            html.Span(
                                "Confirm with CapitalPay support - refreshes every 3 s",
                                style={
                                    "marginLeft": "auto",
                                    "fontSize": "11px",
                                    "color": MUTED,
                                },
                            ),
                        ],
                    ),
                    html.Div(
                        id="ip-table",
                        style={
                            "background": SURF,
                            "border": f"1px solid {BORD}",
                            "borderRadius": "13px",
                            "overflow": "hidden",
                        },
                    ),
                ],
            ),
        ]
    )


def _event_feed(events):
    if not events:
        return html.Div(
            [
                html.Div("No notifications yet", style={"fontSize": "18px", "marginBottom": "10px"}),
                html.P(
                    "Point CapitalPay notification_url to /notify.",
                    style={"fontSize": "13px", "color": MUTED},
                ),
            ],
            style={
                "textAlign": "center",
                "padding": "50px 20px",
                "background": SURF,
                "border": f"1px solid {BORD}",
                "borderRadius": "13px",
            },
        )

    rows = []
    for ev in events:
        status_color, status_label = {
            "settled": (GREEN, "SETTLED"),
            "pending": (WARN, "PENDING"),
            "failed": (RED, "FAILED"),
        }.get(ev["status"].lower(), (MUTED, ev["status"].upper()))
        hash_color, hash_label = (GREEN, "Hash OK") if ev["valid_hash"] else (RED, "Hash mismatch")
        refs = ev.get("payment_references", [])
        refs_text = ", ".join(r.get("payment_reference", "-") for r in refs) if refs else "-"

        rows.append(
            html.Div(
                style={
                    "background": SURF,
                    "border": f"1px solid {BORD}",
                    "borderRadius": "13px",
                    "marginBottom": "12px",
                    "overflow": "hidden",
                },
                children=[
                    html.Div(
                        style={
                            "display": "flex",
                            "alignItems": "center",
                            "gap": "8px",
                            "flexWrap": "wrap",
                            "padding": "12px 18px",
                            "borderBottom": f"1px solid {BORD}",
                            "background": "#1a1f2b",
                        },
                        children=[
                            badge_m(status_label, status_color),
                            badge_m(hash_label, hash_color),
                            badge_m(ev.get("payment_channel", "-")),
                            html.Span(
                                f"{ev['received_at']} | {ev['invoice_number']}",
                                style={"marginLeft": "auto", "fontSize": "11px", "color": MUTED},
                            ),
                        ],
                    ),
                    html.Div(
                        style={"padding": "14px 18px"},
                        children=[
                            html.Div(
                                style={
                                    "display": "grid",
                                    "gridTemplateColumns": "repeat(3, minmax(0, 1fr))",
                                    "gap": "12px",
                                },
                                children=[
                                    html.Div(
                                        [
                                            kv_item_m("Sender IP", ev.get("ip_address")),
                                            kv_item_m("Phone number", ev["phone_number"]),
                                            kv_item_m("Payment channel", ev["payment_channel"]),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            kv_item_m("Client ref", ev["client_invoice_ref"]),
                                            kv_item_m("Invoice number", ev["invoice_number"]),
                                            kv_item_m("Currency", ev["currency"]),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            kv_item_m("Invoice amount", f"{ev['invoice_amount']} TZS"),
                                            kv_item_m("Last payment", f"{ev['last_payment_amount']} TZS"),
                                            kv_item_m("Amount paid", f"{ev['amount_paid']} TZS"),
                                        ]
                                    ),
                                ],
                            ),
                            html.Hr(
                                style={
                                    "border": "none",
                                    "borderTop": f"1px solid {BORD}",
                                    "margin": "12px 0",
                                }
                            ),
                            kv_item_m("Payment references", refs_text),
                            kv_item_m("Payment date", ev["payment_date"]),
                        ],
                    ),
                ],
            )
        )
    return html.Div(rows)


def _ip_table(ip_data):
    th = {
        "textAlign": "left",
        "padding": "8px 12px",
        "fontSize": "10px",
        "color": MUTED,
        "textTransform": "uppercase",
        "letterSpacing": ".07em",
        "background": "#1a1f2b",
        "borderBottom": f"1px solid {BORD}",
    }
    rows = [
        html.Tr(
            [
                html.Td(
                    r["ip_address"],
                    style={
                        "padding": "8px 12px",
                        "fontFamily": "monospace",
                        "color": ACC2,
                        "fontSize": "12px",
                    },
                ),
                html.Td(r["first_seen"], style={"padding": "8px 12px", "color": TEXT, "fontSize": "12px"}),
                html.Td(r["last_seen"], style={"padding": "8px 12px", "color": TEXT, "fontSize": "12px"}),
                html.Td(
                    str(r["hit_count"]),
                    style={
                        "padding": "8px 12px",
                        "textAlign": "center",
                        "fontWeight": "700",
                        "color": TEXT,
                        "fontSize": "12px",
                    },
                ),
            ]
        )
        for r in ip_data
    ] or [
        html.Tr(
            html.Td(
                "No POST requests received yet",
                colSpan=4,
                style={"textAlign": "center", "padding": "22px", "color": MUTED, "fontSize": "12px"},
            )
        )
    ]

    return html.Table(
        style={"width": "100%", "borderCollapse": "collapse"},
        children=[
            html.Thead(
                html.Tr(
                    [
                        html.Th("IP Address", style=th),
                        html.Th("First seen (UTC)", style=th),
                        html.Th("Last seen (UTC)", style=th),
                        html.Th("Hits", style={**th, "textAlign": "center"}),
                    ]
                )
            ),
            html.Tbody(rows),
        ],
    )


def register_monitor_callbacks(app, get_notifications, clear_notifications, _inject_test, get_db):
    @app.callback(
        Output("s-total", "children"),
        Output("s-settled", "children"),
        Output("s-bad", "children"),
        Output("s-amount", "children"),
        Output("m-feed", "children"),
        Output("ip-table", "children"),
        Output("ip-count", "children"),
        Input("interval", "n_intervals"),
        Input("btn-clear-notif", "n_clicks"),
        Input("btn-test-event", "n_clicks"),
        State("m-search", "value"),
        State("m-status", "value"),
        State("m-hash", "value"),
        prevent_initial_call=False,
    )
    def refresh_monitor(_, clr, tst, search, status_f, hash_f):
        ctx = callback_context
        if ctx.triggered:
            trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]
            if trigger_id == "btn-clear-notif":
                clear_notifications()
            elif trigger_id == "btn-test-event":
                _inject_test()

        events = get_notifications()
        settled = [e for e in events if e["status"].lower() == "settled"]
        bad = [e for e in events if not e["valid_hash"]]
        paid = sum(float(e["amount_paid"] or 0) for e in settled)

        filtered = events
        if status_f:
            filtered = [e for e in filtered if e["status"].lower() == status_f]
        if hash_f == "valid":
            filtered = [e for e in filtered if e["valid_hash"]]
        elif hash_f == "invalid":
            filtered = [e for e in filtered if not e["valid_hash"]]
        if search:
            query = search.lower()
            filtered = [
                e
                for e in filtered
                if query in (e["invoice_number"] or "").lower()
                or query in (e["client_invoice_ref"] or "").lower()
                or query in (e["phone_number"] or "").lower()
                or query in (e.get("ip_address") or "").lower()
            ]

        with get_db() as db:
            ip_data = [
                dict(r)
                for r in db.execute(
                    "SELECT ip_address, first_seen, last_seen, hit_count "
                    "FROM capitalpay_ips ORDER BY hit_count DESC, last_seen DESC"
                ).fetchall()
            ]

        return (
            str(len(events)),
            str(len(settled)),
            str(len(bad)),
            f"{paid:,.2f}",
            _event_feed(filtered),
            _ip_table(ip_data),
            f"{len(ip_data)} IP{'s' if len(ip_data) != 1 else ''} observed",
        )
