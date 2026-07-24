# Portfolio blurb

Use these sentences on LinkedIn, your CV, or in interview intros.

## Short (1 sentence)

Built an autonomous sprint agent system that turns Jira tickets into reviewed GitHub pull requests using LangGraph, Pydantic AI, and local vLLM inference with strict schema contracts and a mock-based test suite plus an opt-in end-to-end trap gate.

## Medium (3 sentences)

Sprint Crew v2 is a multi-agent coding pipeline that automates the sprint delivery loop: TechLead plans multi-file changes, Coder implements with tool-using LLMs, Tester and Reviewer validate scope and correctness, and the orchestrator opens a PR for human merge approval. The system uses LangGraph for state management, Pydantic v2 for strict agent contracts, and a dual-lane vLLM setup with on-demand GPU loading. It is covered by a fast mock-based unit suite in CI plus one opt-in end-to-end trap gate that exercises the full pipeline on GPU lanes.

## Bullets for CV “Projects” section

- Designed LangGraph pipeline with deterministic plan coverage gates and smart review retry routing
- Implemented path/command sandboxing and orchestrator-only side effects (git, Jira, GitHub)
- Built a lean test suite: mock-based unit tests (CI) plus one opt-in from-prompt end-to-end trap gate
- Operated dual vLLM lanes (Coder + Work) with single-lane GPU memory policy on 128 GB unified memory
