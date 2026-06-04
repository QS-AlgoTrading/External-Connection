"""Application orchestration for external-strategy paper / live execution.

No exchange APIs, no filesystem reads. Take pure inputs, return pure
outputs. The infrastructure layer feeds in the price + the persisted
state, this layer decides what to do, and the infrastructure layer
saves the result.
"""
