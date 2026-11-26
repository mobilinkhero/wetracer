/*
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

/**
 * How many tag pills fit in a row: the pills' right edges, in order, against
 * the row's right edge. When some pills are hidden, a "+N" pill takes room
 * too, so the count leaves `morePillWidth` for it — the "+N" is always
 * visible, and no pill is ever cut off at the row's edge. Anything else the
 * row reserves (the unread badge) folds into `reserveWidth`.
 */
export function visiblePillCount(
  pillRightEdges: number[],
  containerRight: number,
  totalTags: number,
  morePillWidth = 34,
  reserveWidth = 0,
): number {
  let count = pillRightEdges.length
  while (count > 0) {
    const reserve = reserveWidth + (count < totalTags ? morePillWidth : 0)
    if (pillRightEdges[count - 1] <= containerRight - reserve) break
    count--
  }
  return count
}
