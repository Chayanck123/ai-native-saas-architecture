"""Agents service: agentic orchestration on a large language model.

A planner decomposes the question, an actor calls tools (knowledge search,
calculator, date) in a bounded loop, and a critic gates the answer - routing
weak ones to human review. Token usage becomes usage-based billing events.
"""
