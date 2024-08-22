"""
Copyright 2023-2024 SGLang Team
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""Request policy scheduler"""

import os
import random
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, List, Optional

from sglang.srt.managers.schedule_batch import Req, ScheduleBatch
from sglang.srt.mem_cache.base_prefix_cache import BasePrefixCache
from sglang.srt.mem_cache.radix_cache import TreeNode

# Clip the estimation of max_new_tokens for the request whose max_new_tokens is very large.
# This can prevent the server from being too conservative.
# Note that this only clips the estimation in the scheduler but does not change the stop
# condition. The request can still generate tokens until it hits the unclipped max_new_tokens.
CLIP_MAX_NEW_TOKENS = int(os.environ.get("SGLANG_CLIP_MAX_NEW_TOKENS", "4096"))


class PolicyScheduler:
    def __init__(self, policy: str, tree_cache: BasePrefixCache):
        if tree_cache.disable and policy in ["lpm", "dfs-weight"]:
            # LPM and DFS-weight is meaningless when the tree cache is disabled.
            policy = "fcfs"

        self.policy = policy
        self.tree_cache = tree_cache

    def calc_priority(self, waiting_queue: List[Req]):
        # Compute matched prefix length
        if self.policy in ["lpm", "dfs-weight"]:
            for r in waiting_queue:
                # NOTE: the prefix_indices must always be aligned with last_node
                r.prefix_indices, r.last_node = self.tree_cache.match_prefix(
                    rid=r.rid, key=r.adjust_max_prefix_ids()
                )

        if self.policy == "lpm":
            # Longest Prefix Match
            waiting_queue.sort(key=lambda x: -len(x.prefix_indices))
        elif self.policy == "fcfs":
            # first come first serve
            pass
        elif self.policy == "lof":
            # longest output first
            waiting_queue.sort(key=lambda x: -x.sampling_params.max_new_tokens)
        elif self.policy == "random":
            random.shuffle(waiting_queue)
        elif self.policy == "dfs-weight":
            last_node_to_reqs = defaultdict(list)
            for req in waiting_queue:
                last_node_to_reqs[req.last_node].append(req)

            node_to_weight = defaultdict(int)
            for node in last_node_to_reqs:
                node_to_weight[node] = len(last_node_to_reqs[node])
            self.calc_weight(self.tree_cache.root_node, node_to_weight)

            waiting_queue.clear()
            self.get_dfs_priority(
                self.tree_cache.root_node,
                node_to_weight,
                last_node_to_reqs,
                waiting_queue,
            )
        else:
            raise ValueError(f"Unknown schedule_policy: {self.policy}")

    def calc_weight(self, cur_node: TreeNode, node_to_weight: Dict):
        for child in cur_node.children.values():
            self.calc_weight(child, node_to_weight)
            node_to_weight[cur_node] += node_to_weight[child]

    def get_dfs_priority(
        self,
        cur_node: TreeNode,
        node_to_priority: Dict,
        last_node_to_reqs: Dict,
        q: List,
    ):
        childs = [child for child in cur_node.children.values()]
        childs.sort(key=lambda x: -node_to_priority[x])
        for child in childs:
            self.get_dfs_priority(child, node_to_priority, last_node_to_reqs, q)
        q.extend(last_node_to_reqs[cur_node])


class PrefillAdder:
    def __init__(
        self,
        tree_cache: BasePrefixCache,
        rem_total_tokens: int,
        rem_input_tokens: int,
        rem_chunk_tokens: Optional[int],
        mixed_with_decode_tokens: int = 0,
    ):
        self.tree_cache = tree_cache
        self.rem_total_tokens = rem_total_tokens - mixed_with_decode_tokens
        self.rem_input_tokens = rem_input_tokens - mixed_with_decode_tokens
        self.rem_chunk_tokens = rem_chunk_tokens
        if self.rem_chunk_tokens is not None:
            self.rem_chunk_tokens -= mixed_with_decode_tokens

        self.can_run_list = []
        self.new_inflight_req = None
        self.log_hit_tokens = 0
        self.log_input_tokens = 0

    def no_remaining_tokens(self):
        return (
            self.rem_total_tokens <= 0
            or self.rem_input_tokens <= 0
            or (
                self.rem_chunk_tokens <= 0
                if self.rem_chunk_tokens is not None
                else False
            )
        )

    def remove_running_tokens(
        self, running_batch: ScheduleBatch, new_token_ratio: float
    ):
        self.rem_total_tokens -= sum(
            [
                min(
                    (r.sampling_params.max_new_tokens - len(r.output_ids)),
                    CLIP_MAX_NEW_TOKENS,
                )
                * new_token_ratio
                for r in running_batch.reqs
            ]
        )

    def _prefill_one_req(
        self, prefix_len: int, extend_input_len: int, max_new_tokens: int
    ):
        self.rem_total_tokens -= extend_input_len + max_new_tokens
        self.rem_input_tokens -= extend_input_len
        if self.rem_chunk_tokens is not None:
            self.rem_chunk_tokens -= extend_input_len

        self.log_hit_tokens += prefix_len
        self.log_input_tokens += extend_input_len

    def add_inflight_req(self, req: Req):
        truncated = req.extend_input_len > self.rem_chunk_tokens
        req.extend_input_len = min(req.extend_input_len, self.rem_chunk_tokens)
        req.fill_ids = req.fill_ids[: len(req.prefix_indices) + req.extend_input_len]
        self.can_run_list.append(req)

        self._prefill_one_req(
            len(req.prefix_indices),
            req.extend_input_len,
            (
                min(req.sampling_params.max_new_tokens, CLIP_MAX_NEW_TOKENS)
                if not truncated
                else 0
            ),
        )

        # Return if chunked prefill not finished
        return req if truncated else None

    @contextmanager
    def _lock_node(self, last_node: TreeNode):
        try:
            delta = self.tree_cache.inc_lock_ref(last_node)
            self.rem_total_tokens += delta
            yield None
        finally:
            delta = self.tree_cache.dec_lock_ref(last_node)
            self.rem_total_tokens += delta

    @contextmanager
    def _lock_req(self, req: Req):
        # match prefix again and lock the last node to prevent data racing
        req.fill_ids = req.origin_input_ids + req.output_ids
        req.prefix_indices, req.last_node, delta = self.tree_cache.match_prefix_lock(
            rid=req.rid, key=req.adjust_max_prefix_ids()
        )
        req.extend_input_len = len(req.fill_ids) - len(req.prefix_indices)
        try:
            self.rem_total_tokens += delta
            yield None
        finally:
            delta = self.tree_cache.dec_lock_ref(req.last_node)
            self.rem_total_tokens += delta

    def add_one_req(self, req: Req):

        with self._lock_req(req):
            total_tokens = req.extend_input_len + min(
                req.sampling_params.max_new_tokens, CLIP_MAX_NEW_TOKENS
            )
            input_tokens = req.extend_input_len
            prefix_len = len(req.prefix_indices)

            if total_tokens >= self.rem_total_tokens:
                return False

            if input_tokens > self.rem_input_tokens and len(self.can_run_list) != 0:
                return False

            if (
                self.rem_chunk_tokens is None
                or input_tokens <= self.rem_chunk_tokens
                or (req.return_logprob and req.normalized_prompt_logprob is None)
            ):
                # Non-chunked prefill
                self.can_run_list.append(req)
                self.tree_cache.inc_lock_ref(req.last_node)
                self._prefill_one_req(
                    prefix_len,
                    input_tokens,
                    min(req.sampling_params.max_new_tokens, CLIP_MAX_NEW_TOKENS),
                )
            else:
                # Chunked prefill
                trunc_len = self.rem_chunk_tokens
                if trunc_len == 0:
                    return False

                req.extend_input_len = trunc_len
                req.fill_ids = req.fill_ids[: len(req.prefix_indices) + trunc_len]
                self.can_run_list.append(req)
                self.new_inflight_req = req
                self.tree_cache.inc_lock_ref(req.last_node)
                self._prefill_one_req(prefix_len, trunc_len, 0)

        return True
