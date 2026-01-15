---
version: "1.1"
lastUpdated: "2026-01-15 04:08 UTC"
lifecycle: "execution"
stakeholder: "all"
changeTrigger: "Asha initialization"
validatedBy: "ai"
dependencies: []
---

# projectbrief

## Project Overview

**asha-marketplace** is a Claude Code plugin marketplace providing tools for multi-perspective analysis, code review, output styling, and session coordination. It serves as both a functional plugin collection and a reference implementation for Claude Code plugin development.

### Core Philosophy

1. **Separation of Concerns**: Framework tells Claude to READ Memory; plugins tell Claude HOW TO MAINTAIN Memory
2. **Portability First**: Memory files must be self-contained and portable across projects
3. **Multi-Session Continuity**: Memory is the ONLY connection between sessions
4. **Character-Based Design**: Separate narrative personas from technical implementation

## Current Primary Objective

### Plugin Ecosystem Maintenance

**Priority**: MEDIUM
**Status**: Stable, active development
**Key Files**: plugins/*/plugin.json, CLAUDE.md

### Goals

1. **Plugin Quality**: Maintain stable, well-documented plugins
2. **Memory System**: Enable effective cross-session context preservation

## Completed Achievements

### Major Milestones

- **2026-01-15**: Asha framework self-initialization complete
- **v1.3.0**: Panel system with dynamic specialist recruitment
- **v1.2.0**: Asha plugin with Vector DB and ReasoningBank support

## Success Metrics

### Completion Benchmarks

- Plugin installation works via Claude Code CLI
- Memory Bank enables session continuity
- Vector DB provides semantic code search

### Quality Validation Criteria

- All plugin.json files valid
- Commands execute without errors
- Hooks trigger correctly

## Available Resources

### Documentation

- **CLAUDE.md**: Comprehensive repository guide
- **plugins/*/README.md**: Per-plugin documentation
- **templates/**: Memory Bank file templates

## Project Scope

### Immediate Deliverables

1. Stable plugin marketplace
2. Session coordination via Memory Bank
3. Semantic search via Vector DB

### Long-term Vision

- Expand plugin ecosystem with community contributions
- Refine Memory Bank patterns for broader adoption

## Key Stakeholders

### Primary User

- Claude Code users seeking enhanced workflow tools
- Developers building on the plugin framework

### Quality Standards

- MIT licensed, open source
- Follow Claude Code plugin conventions
- Maintain backwards compatibility

## Critical Context

This repository is both a product (usable plugins) and a reference (how to build plugins). Changes should consider both audiences.
