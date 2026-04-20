#pragma once
/**
 * Treap with order statistics for streaming TsRank.
 *
 * Key idea: store (value, unique_seq_id) pairs so every element is unique.
 * The Treap maintains BST order and supports:
 *   - insert:        O(log n) expected
 *   - erase:         O(log n) expected
 *   - count_less:    O(log n) expected
 *   - count_equal:   derived from two queries
 *
 * Memory: pre-allocated node pool with free-list recycling.
 * At most `window` nodes are live at any time.
 */

#include <cstdint>
#include <limits>
#include <vector>

#include "fe/ops/spec.hpp"

namespace fe::ops {

class TreapOrderStatistic {
public:
    explicit TreapOrderStatistic(int max_size)
        : pool_(max_size + 2), root_(-1), pool_size_(max_size + 2), free_head_(-1) {
        rng_state_ = 0xDEADBEEF42ULL;
        for (int i = max_size + 1; i >= 0; --i) {
            pool_[i].next_free = free_head_;
            free_head_ = i;
        }
    }

    void insert(FeFloat value, std::uint64_t seq) {
        int nd = alloc_node(value, seq);
        root_ = insert_node(root_, nd);
    }

    void erase(FeFloat value, std::uint64_t seq) {
        root_ = erase_node(root_, value, seq);
    }

    int count_less(FeFloat value) const {
        int cnt = 0;
        int cur = root_;
        while (cur >= 0) {
            auto& n = pool_[cur];
            if (value <= n.value) {
                cur = n.left;
            } else {
                cnt += size_of(n.left) + 1;
                cur = n.right;
            }
        }
        return cnt;
    }

    int count_less_or_equal(FeFloat value) const {
        int cnt = 0;
        int cur = root_;
        while (cur >= 0) {
            auto& n = pool_[cur];
            if (value < n.value) {
                cur = n.left;
            } else {
                cnt += size_of(n.left) + 1;
                cur = n.right;
            }
        }
        return cnt;
    }

    int count_equal(FeFloat value) const {
        return count_less_or_equal(value) - count_less(value);
    }

    int size() const { return size_of(root_); }

    void clear() {
        root_ = -1;
        free_head_ = -1;
        for (int i = pool_size_ - 1; i >= 0; --i) {
            pool_[i].next_free = free_head_;
            free_head_ = i;
        }
    }

private:
    struct Node {
        FeFloat value;
        std::uint64_t seq;
        std::uint32_t priority;
        int left, right;
        int subtree_size;
        int next_free;
    };

    std::vector<Node> pool_;
    int root_;
    int pool_size_;
    int free_head_;
    std::uint64_t rng_state_;

    std::uint32_t next_rand() {
        rng_state_ ^= rng_state_ << 13;
        rng_state_ ^= rng_state_ >> 7;
        rng_state_ ^= rng_state_ << 17;
        return static_cast<std::uint32_t>(rng_state_);
    }

    int size_of(int idx) const {
        return idx < 0 ? 0 : pool_[idx].subtree_size;
    }

    void update_size(int idx) {
        if (idx >= 0)
            pool_[idx].subtree_size = 1 + size_of(pool_[idx].left) + size_of(pool_[idx].right);
    }

    static bool key_less(FeFloat av, std::uint64_t as, FeFloat bv, std::uint64_t bs) {
        return av < bv || (av == bv && as < bs);
    }

    int alloc_node(FeFloat value, std::uint64_t seq) {
        int idx = free_head_;
        free_head_ = pool_[idx].next_free;
        auto& n = pool_[idx];
        n.value = value;
        n.seq = seq;
        n.priority = next_rand();
        n.left = -1;
        n.right = -1;
        n.subtree_size = 1;
        return idx;
    }

    void free_node(int idx) {
        pool_[idx].next_free = free_head_;
        free_head_ = idx;
    }

    void split(int t, FeFloat val, std::uint64_t seq, int& l, int& r) {
        if (t < 0) { l = r = -1; return; }
        if (key_less(pool_[t].value, pool_[t].seq, val, seq)) {
            split(pool_[t].right, val, seq, pool_[t].right, r);
            l = t;
        } else {
            split(pool_[t].left, val, seq, l, pool_[t].left);
            r = t;
        }
        update_size(t);
    }

    int merge(int l, int r) {
        if (l < 0) return r;
        if (r < 0) return l;
        if (pool_[l].priority > pool_[r].priority) {
            pool_[l].right = merge(pool_[l].right, r);
            update_size(l);
            return l;
        } else {
            pool_[r].left = merge(l, pool_[r].left);
            update_size(r);
            return r;
        }
    }

    int insert_node(int t, int nd) {
        int l, r;
        split(t, pool_[nd].value, pool_[nd].seq, l, r);
        return merge(merge(l, nd), r);
    }

    int erase_node(int t, FeFloat val, std::uint64_t seq) {
        if (t < 0) return -1;
        if (pool_[t].value == val && pool_[t].seq == seq) {
            int result = merge(pool_[t].left, pool_[t].right);
            free_node(t);
            return result;
        }
        if (key_less(val, seq, pool_[t].value, pool_[t].seq)) {
            pool_[t].left = erase_node(pool_[t].left, val, seq);
        } else {
            pool_[t].right = erase_node(pool_[t].right, val, seq);
        }
        update_size(t);
        return t;
    }
};

}  // namespace fe::ops
