---
type: decision
title: "ADR 0001: Keep project knowledge in an OKF bundle"
description: "Project knowledge lives in an OKF bundle in .okf/, next to the code, not in a wiki or README notes."
tags: [decision]
generated:
  by: process:kmctl/0.18.0
  at: 2026-11-27T06:51:37Z
status: stable
---

# ADR 0001: Keep project knowledge in an OKF bundle

## Context

This repository needs a place for knowledge that people and agents working on
it can find and trust: what the project is for, why each choice was made, and
how the design fits together. Knowledge kept in someone's head or in chat is
lost when they move on, and an agent starting a session cannot read it.

## Decision

Keep project knowledge in an OKF bundle in `.okf/`, set up and checked by
kmctl, next to the code it describes. Each choice gets a decision record when
it is made, the design gets diagrams in `.okf/architecture/`, and `docs/`
holds pages for people who use the project.

Rejected:

- A wiki. It lives apart from the code, so it drifts, and an agent cannot read
  it in a session.
- README-only notes. One file mixes what users and maintainers need and loses
  the why of each choice.
- Nothing. Every new session and every new person finds the same reasons
  again, or acts without them.

## Consequences

- Each change that makes a choice also writes a decision record, and a change
  to the design updates its architecture page.
- `kmctl check` keeps the indexes and links in step, so a reader can trust
  them.
- A record costs a few minutes per choice and saves rereading the code to
  find out why.
