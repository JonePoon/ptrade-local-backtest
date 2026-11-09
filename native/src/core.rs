//! Python-independent indexing and allocation accounting.

use std::mem::{size_of, size_of_val};

struct CodeRange {
    code: Box<str>,
    start: usize,
    end: usize,
}

pub(crate) struct IndexCore {
    slots: Box<[i32]>,
    // A half-full, owned open-addressed table makes its allocation measurable.
    // No borrowed Python keys/arrays and no opaque HashMap allocation estimate.
    entries: Box<[Option<CodeRange>]>,
    slot_count: Option<usize>,
    ncodes: usize,
}

fn hash_code(code: &str) -> usize {
    // FNV-1a is inexpensive for the short, non-adversarial exchange code keys.
    // Expected lookup O(1); pathological collision chains are O(number of codes).
    let mut hash = 0xcbf2_9ce4_8422_2325_u64;
    for byte in code.bytes() {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash as usize
}

impl IndexCore {
    pub(crate) fn new(
        slots: Box<[i32]>,
        ranges: Vec<(String, (isize, isize))>,
        slot_count: Option<usize>,
    ) -> Result<Self, String> {
        if slot_count == Some(0) {
            return Err("slot_count must be positive when supplied".to_owned());
        }
        let ncodes = ranges.len();
        let capacity = if ncodes == 0 {
            0
        } else {
            ncodes
                .checked_mul(2)
                .and_then(usize::checked_next_power_of_two)
                .ok_or_else(|| "too many code ranges".to_owned())?
        };
        let entries = std::iter::repeat_with(|| None)
            .take(capacity)
            .collect::<Vec<_>>()
            .into_boxed_slice();
        let mut index = Self {
            slots,
            entries,
            slot_count,
            ncodes,
        };

        // O(N + C) for the ordinary disjoint day ranges. Overlapping ranges are
        // allowed like independent numpy slices, costing O(sum(range lengths)).
        for (code, (start, end)) in ranges {
            let start =
                usize::try_from(start).map_err(|_| format!("negative range start for {code:?}"))?;
            let end =
                usize::try_from(end).map_err(|_| format!("negative range end for {code:?}"))?;
            if start > end || end > index.slots.len() {
                return Err(format!(
                    "invalid range ({start}, {end}) for {code:?}: nrows={}",
                    index.slots.len()
                ));
            }
            if index.slots[start..end]
                .windows(2)
                .any(|pair| pair[0] > pair[1])
            {
                return Err(format!("slots are not sorted within code {code:?}"));
            }
            // slot_count is axis metadata, not a clamp: comparison must retain
            // numpy searchsorted behavior for every representable slot value.
            let mut bucket = hash_code(&code) & (index.entries.len() - 1);
            loop {
                match &index.entries[bucket] {
                    Some(entry) if entry.code.as_ref() == code => {
                        return Err(format!("duplicate code key {code:?}"));
                    }
                    Some(_) => bucket = (bucket + 1) & (index.entries.len() - 1),
                    None => {
                        index.entries[bucket] = Some(CodeRange {
                            code: code.into_boxed_str(),
                            start,
                            end,
                        });
                        break;
                    }
                }
            }
        }
        Ok(index)
    }

    fn range_of(&self, code: &str) -> Option<(usize, usize)> {
        if self.entries.is_empty() {
            return None;
        }
        let mask = self.entries.len() - 1;
        let mut bucket = hash_code(code) & mask;
        loop {
            match &self.entries[bucket] {
                Some(entry) if entry.code.as_ref() == code => {
                    return Some((entry.start, entry.end));
                }
                Some(_) => bucket = (bucket + 1) & mask,
                None => return None,
            }
        }
    }

    pub(crate) fn row_of(&self, code: &str, slot: i64) -> isize {
        let Some((start, end)) = self.range_of(code) else {
            return -1;
        };
        let values = &self.slots[start..end];
        // lower_bound, not binary_search: duplicate slots resolve to FIRST row.
        let offset = values.partition_point(|value| i64::from(*value) < slot);
        if offset < values.len() && i64::from(values[offset]) == slot {
            (start + offset) as isize
        } else {
            -1
        }
    }

    pub(crate) fn rows_upto(&self, code: &str, slot: i64, include: bool) -> (usize, usize) {
        let Some((start, end)) = self.range_of(code) else {
            return (0, 0);
        };
        let values = &self.slots[start..end];
        let offset = if include {
            values.partition_point(|value| i64::from(*value) <= slot)
        } else {
            values.partition_point(|value| i64::from(*value) < slot)
        };
        (start, start + offset)
    }

    pub(crate) fn rows_of(&self, codes: &[String], slot: i64) -> Vec<isize> {
        codes.iter().map(|code| self.row_of(code, slot)).collect()
    }

    pub(crate) fn nbytes(&self) -> usize {
        // Exact owned allocation payload, excluding allocator bookkeeping,
        // Python wrapper overhead and temporary query/result allocations.
        size_of::<Self>()
            + size_of_val(self.slots.as_ref())
            + size_of_val(self.entries.as_ref())
            + self
                .entries
                .iter()
                .filter_map(Option::as_ref)
                .map(|entry| entry.code.len())
                .sum::<usize>()
    }

    pub(crate) fn nrows(&self) -> usize {
        self.slots.len()
    }

    pub(crate) fn ncodes(&self) -> usize {
        self.ncodes
    }

    pub(crate) fn slot_count(&self) -> Option<usize> {
        self.slot_count
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn small_index() -> IndexCore {
        IndexCore::new(
            vec![0, 2, 2, 7, 240, 1, 1, 3].into_boxed_slice(),
            vec![
                ("a".to_owned(), (0, 5)),
                ("b".to_owned(), (5, 8)),
                ("empty".to_owned(), (4, 4)),
            ],
            Some(241),
        )
        .unwrap()
    }

    #[test]
    fn exact_lookup_returns_first_duplicate_and_never_a_nearby_bar() {
        let index = small_index();
        assert_eq!(index.row_of("a", 0), 0);
        assert_eq!(index.row_of("a", 2), 1);
        assert_eq!(index.row_of("a", 3), -1);
        assert_eq!(index.row_of("a", 240), 4);
        assert_eq!(index.row_of("b", 1), 5);
        for code in ["a", "b", "empty", "unknown"] {
            assert_eq!(index.row_of(code, -1), -1);
            assert_eq!(index.row_of(code, 241), -1);
        }
    }

    #[test]
    fn left_and_right_bounds_include_all_or_none_of_duplicate_slots() {
        let index = small_index();
        assert_eq!(index.rows_upto("a", 2, false), (0, 1));
        assert_eq!(index.rows_upto("a", 2, true), (0, 3));
        assert_eq!(index.rows_upto("b", 1, false), (5, 5));
        assert_eq!(index.rows_upto("b", 1, true), (5, 7));
        assert_eq!(index.rows_upto("b", -1, true), (5, 5));
        assert_eq!(index.rows_upto("b", 241, false), (5, 8));
        assert_eq!(index.rows_upto("empty", 100, true), (4, 4));
        assert_eq!(index.rows_upto("unknown", 100, true), (0, 0));
    }

    #[test]
    fn batch_preserves_input_order_duplicate_codes_and_missing_codes() {
        let index = small_index();
        let codes = ["b", "missing", "a", "b", "empty"]
            .map(str::to_owned)
            .to_vec();
        assert_eq!(index.rows_of(&codes, 1), vec![5, -1, -1, 5, -1]);
        assert!(index.rows_of(&[], 1).is_empty());
    }

    #[test]
    fn int32_extrema_and_wider_queries_are_not_narrowed_or_wrapped() {
        let index = IndexCore::new(
            vec![i32::MIN, -1, 0, 0, i32::MAX].into_boxed_slice(),
            vec![("a".to_owned(), (0, 5))],
            Some(1),
        )
        .unwrap();
        assert_eq!(index.row_of("a", i64::from(i32::MIN)), 0);
        assert_eq!(index.row_of("a", 0), 2);
        assert_eq!(index.row_of("a", i64::from(i32::MAX)), 4);
        for query in [i64::MIN, i64::from(i32::MIN) - 1] {
            assert_eq!(index.row_of("a", query), -1);
            assert_eq!(index.rows_upto("a", query, true), (0, 0));
        }
        for query in [i64::MAX, i64::from(i32::MAX) + 1] {
            assert_eq!(index.row_of("a", query), -1);
            assert_eq!(index.rows_upto("a", query, false), (0, 5));
        }
    }

    #[test]
    fn zero_rows_and_no_code_ranges_are_valid() {
        let index = IndexCore::new(Box::new([]), vec![], None).unwrap();
        assert_eq!(index.nrows(), 0);
        assert_eq!(index.ncodes(), 0);
        assert_eq!(index.row_of("missing", 0), -1);
        assert_eq!(index.rows_upto("missing", 0, true), (0, 0));
        assert_eq!(index.nbytes(), size_of::<IndexCore>());
    }

    #[test]
    fn empty_and_overlapping_ranges_retain_independent_slice_semantics() {
        let index = IndexCore::new(
            vec![0, 2, 2, 7, 240].into_boxed_slice(),
            vec![
                ("whole".to_owned(), (0, 5)),
                ("part".to_owned(), (2, 4)),
                ("empty".to_owned(), (5, 5)),
            ],
            None,
        )
        .unwrap();
        assert_eq!(index.row_of("whole", 2), 1);
        assert_eq!(index.row_of("part", 2), 2);
        assert_eq!(index.rows_upto("part", 2, false), (2, 2));
        assert_eq!(index.rows_upto("empty", 999, true), (5, 5));
    }

    #[test]
    fn malformed_bounds_and_unsorted_ranges_fail_construction() {
        for bounds in [(-1, 1), (0, -1), (2, 1), (0, 4)] {
            assert!(IndexCore::new(
                vec![0, 1, 2].into_boxed_slice(),
                vec![("bad".to_owned(), bounds)],
                None,
            )
            .is_err());
        }
        assert!(IndexCore::new(
            vec![2, 1].into_boxed_slice(),
            vec![("bad".to_owned(), (0, 2))],
            None,
        )
        .is_err());
        // Unindexed rows need not be globally sorted.
        assert!(IndexCore::new(
            vec![2, 1, 0].into_boxed_slice(),
            vec![("one".to_owned(), (1, 2))],
            None,
        )
        .is_ok());
    }

    #[test]
    fn duplicate_keys_and_zero_axis_length_fail_construction() {
        assert!(IndexCore::new(
            vec![0].into_boxed_slice(),
            vec![("a".to_owned(), (0, 1)), ("a".to_owned(), (0, 1))],
            None,
        )
        .is_err());
        assert!(IndexCore::new(Box::new([]), vec![], Some(0)).is_err());
        assert_eq!(small_index().slot_count(), Some(241));
    }

    #[test]
    fn code_table_handles_many_keys_and_utf8_codes() {
        let slots = vec![7; 1025].into_boxed_slice();
        let mut ranges = (0..1024)
            .map(|i| (format!("{i:06}.SZ"), (i, i + 1)))
            .collect::<Vec<_>>();
        ranges.push(("中文代码".to_owned(), (1024, 1025)));
        let index = IndexCore::new(slots, ranges, None).unwrap();
        for i in 0..1024 {
            assert_eq!(index.row_of(&format!("{i:06}.SZ"), 7), i);
        }
        assert_eq!(index.row_of("中文代码", 7), 1024);
        assert_eq!(index.row_of("missing", 7), -1);
    }

    #[test]
    fn allocation_accounting_counts_slot_copy_and_utf8_key_bytes() {
        let build = |n, code: &str| {
            IndexCore::new(
                vec![0; n].into_boxed_slice(),
                vec![(code.to_owned(), (0, n as isize))],
                None,
            )
            .unwrap()
        };
        assert_eq!(build(101, "a").nbytes() - build(1, "a").nbytes(), 400);
        assert_eq!(build(1, "中文").nbytes() - build(1, "ab").nbytes(), 4);
        let index = small_index();
        let before = index.nbytes();
        for query in -10..300 {
            index.row_of("a", query);
            index.rows_upto("b", query, true);
        }
        assert_eq!(index.nbytes(), before);
    }

    #[test]
    fn random_sorted_ranges_match_an_independent_linear_reference() {
        let mut seed = 0x2019_0512_u64;
        let mut slots = Vec::new();
        let mut ranges = Vec::new();
        for i in 0..47 {
            let start = slots.len();
            let mut values = Vec::new();
            for _ in 0..i {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                values.push(((seed >> 32) % 260) as i32 - 10);
            }
            values.sort_unstable();
            slots.extend(values);
            ranges.push((format!("code-{i}"), (start as isize, slots.len() as isize)));
        }
        let index =
            IndexCore::new(slots.clone().into_boxed_slice(), ranges.clone(), Some(241)).unwrap();
        for (code, (start, end)) in &ranges {
            let (start, end) = (*start as usize, *end as usize);
            for query in -12..263 {
                let values = &slots[start..end];
                let expected = values
                    .iter()
                    .position(|value| i64::from(*value) == query)
                    .map_or(-1, |offset| (start + offset) as isize);
                assert_eq!(index.row_of(code, query), expected);
                for include in [false, true] {
                    let count = values
                        .iter()
                        .filter(|value| {
                            let value = i64::from(**value);
                            value < query || (include && value == query)
                        })
                        .count();
                    assert_eq!(
                        index.rows_upto(code, query, include),
                        (start, start + count)
                    );
                }
            }
        }
    }

    #[test]
    fn immutable_index_supports_concurrent_readers_without_query_state() {
        let index = Arc::new(small_index());
        let readers = (0..4)
            .map(|_| {
                let index = Arc::clone(&index);
                std::thread::spawn(move || {
                    for _ in 0..1000 {
                        assert_eq!(index.row_of("a", 2), 1);
                        assert_eq!(index.rows_upto("b", 1, true), (5, 7));
                    }
                })
            })
            .collect::<Vec<_>>();
        for reader in readers {
            reader.join().unwrap();
        }
    }
}
