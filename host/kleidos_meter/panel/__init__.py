# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel on the PC: ``kleidos-meter serve`` and what it is built from.

One process owns the meter's port (:mod:`~kleidos_meter.panel.serve`), and
every program message goes through its executor
(:mod:`~kleidos_meter.panel.executor`): one thread, a FIFO, one message in
flight. :mod:`~kleidos_meter.panel.shaping` keeps the capture-shaping settings
serve found and puts back what it changed; :mod:`~kleidos_meter.panel.lockinfo`
names the process that holds a port to the next one that asks for it.
:mod:`~kleidos_meter.panel.http` serves the HTTP API on the loopback, behind
the accident guards of :mod:`~kleidos_meter.panel.guards`, and
:mod:`~kleidos_meter.panel.schema` declares once what it offers;
:mod:`~kleidos_meter.panel.reports` turns the instrument's reports into its
JSON. :mod:`~kleidos_meter.panel.live` streams the live samples and events
to the page as Server-Sent Events from a bounded ring, and
:mod:`~kleidos_meter.panel.recorder` records every capture serve owns into a
run directory and reads runs back as exports and decimations.
:mod:`~kleidos_meter.panel.settings` writes the settings and shows each
range's shunt with the registry's uncertainty, takes an override only with
its own and reverts to the registry; :mod:`~kleidos_meter.panel.sequences`
runs the measurement sequences that :mod:`~kleidos_meter.panel.steps`
declares as data, one at a time, their cleanup always.
:mod:`~kleidos_meter.panel.passthrough` gives the line to one SCPI client at a
time on the loopback, byte for byte, while the panel watches.
:mod:`~kleidos_meter.panel.static` serves the page itself: the files under
``static/``, plain HTML, one stylesheet and ES modules, read once and
served from the panel's own origin with nothing fetched from another.
:mod:`~kleidos_meter.panel.local_elf` takes the local build's ``elf=``, which
the Calibration and health view holds the instrument's to, and
:mod:`~kleidos_meter.panel.paths` names a local file to the page and the API
by no absolute path.
"""
