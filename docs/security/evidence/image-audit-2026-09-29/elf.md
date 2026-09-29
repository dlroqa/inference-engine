Completeness: **complete**. Regular files examined 7843; ELF discovered 814, inspected 814, unassessed 0; inspected without a dynamic symbol table 1.

- no `.dynsym` (imports not visible): `/usr/local/lib/python3.12/config-3.12-x86_64-linux-gnu/python.o`

| Group | Symbol | Defined in | Imported by |
| --- | --- | --- | --- |
| zlib: CVE-2026-85091 gz write path | `gzwrite` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | /usr/bin/dpkg-deb, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0 |
| zlib: CVE-2026-85091 gz write path | `gzfwrite` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzprintf` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzvprintf` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzputc` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzputs` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzflush` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzsetparams` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-85091 gz write path | `gzclose_w` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: gzFile open (any gz* use) | `gzopen` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: gzFile open (any gz* use) | `gzopen64` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: gzFile open (any gz* use) | `gzdopen` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | /usr/bin/dpkg-deb, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0 |
| zlib: gzFile open (any gz* use) | `gzopen_w` | - | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-27171 crc32 combine | `crc32_combine` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-27171 crc32 combine | `crc32_combine64` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-27171 crc32 combine | `crc32_combine_gen` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-27171 crc32 combine | `crc32_combine_gen64` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| zlib: CVE-2026-27171 crc32 combine | `crc32_combine_op` | /usr/lib/x86_64-linux-gnu/libz.so.1.3.1 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-19499 strfmon | `strfmon` | /usr/lib/x86_64-linux-gnu/libc.so.6 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-19499 strfmon | `strfmon_l` | /usr/lib/x86_64-linux-gnu/libc.so.6 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `ns_printrr` | - | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `ns_printrrf` | - | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `ns_sprintrr` | /usr/lib/x86_64-linux-gnu/libresolv.so.2 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `ns_sprintrrf` | /usr/lib/x86_64-linux-gnu/libresolv.so.2 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `fp_nquery` | - | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `fp_query` | - | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-5435/6238 ns_printrr/fp_nquery | `p_query` | - | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-19542 tdelete | `tdelete` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/lib/x86_64-linux-gnu/libncursesw.so.6.5, /usr/lib/x86_64-linux-gnu/libtinfo.so.6.5 |
| glibc: CVE-2026-6791/6368 wordexp | `wordexp` | /usr/lib/x86_64-linux-gnu/libc.so.6 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-6791/6368 wordexp | `wordfree` | /usr/lib/x86_64-linux-gnu/libc.so.6 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2026-77117/80489 iconv (JISX0213) | `iconv_open` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/bin/bash, /usr/bin/cmp, /usr/bin/diff, /usr/bin/diff3, /usr/bin/iconv, /usr/bin/printf, /usr/bin/sdiff, /usr/bin/tar, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0, /usr/lib/x86_64-linux-gnu/libstdc++.so.6.0.33 |
| glibc: CVE-2026-77117/80489 iconv (JISX0213) | `iconv` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/bin/bash, /usr/bin/cmp, /usr/bin/diff, /usr/bin/diff3, /usr/bin/iconv, /usr/bin/printf, /usr/bin/sdiff, /usr/bin/tar, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0, /usr/lib/x86_64-linux-gnu/libstdc++.so.6.0.33 |
| glibc: CVE-2010-4756 glob | `glob` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/bin/tar, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0, /usr/lib/x86_64-linux-gnu/security/pam_access.so, /usr/lib/x86_64-linux-gnu/security/pam_limits.so, /usr/lib/x86_64-linux-gnu/security/pam_namespace.so |
| glibc: CVE-2010-4756 glob | `glob64` | /usr/lib/x86_64-linux-gnu/libc.so.6 | no direct dynamic-symbol import in the 814 inspected files |
| glibc: CVE-2018-20796/2019-9192 regexec | `regexec` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/bin/bash, /usr/bin/diff, /usr/bin/du, /usr/bin/grep, /usr/bin/hardlink, /usr/bin/more, /usr/bin/run-parts, /usr/bin/tar, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0, /usr/lib/x86_64-linux-gnu/libapt-private.so.0.0.0, /usr/lib/x86_64-linux-gnu/libformw.so.6.5, /usr/lib/x86_64-linux-gnu/libsemanage.so.2, /usr/lib/x86_64-linux-gnu/libsmartcols.so.1.1.0 |
| glibc: CVE-2018-20796/2019-9192 regexec | `regcomp` | /usr/lib/x86_64-linux-gnu/libc.so.6 | /usr/bin/bash, /usr/bin/diff, /usr/bin/du, /usr/bin/grep, /usr/bin/hardlink, /usr/bin/localedef, /usr/bin/more, /usr/bin/run-parts, /usr/bin/tar, /usr/lib/x86_64-linux-gnu/libapt-pkg.so.7.0.0, /usr/lib/x86_64-linux-gnu/libapt-private.so.0.0.0, /usr/lib/x86_64-linux-gnu/libformw.so.6.5, /usr/lib/x86_64-linux-gnu/libsemanage.so.2, /usr/lib/x86_64-linux-gnu/libsmartcols.so.1.1.0 |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3session_create` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changeset_apply` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changeset_apply_v2` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changeset_apply_v3` | - | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changeset_concat` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changegroup_new` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: CVE-2026-50812/50813 session extension | `sqlite3changegroup_add` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | no direct dynamic-symbol import in the 814 inspected files |
| sqlite: extension loading (zipfile/session at runtime) | `sqlite3_load_extension` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | /usr/local/lib/python3.12/lib-dynload/_sqlite3.cpython-312-x86_64-linux-gnu.so |
| sqlite: extension loading (zipfile/session at runtime) | `sqlite3_enable_load_extension` | /usr/lib/x86_64-linux-gnu/libsqlite3.so.0.8.6 | /usr/local/lib/python3.12/lib-dynload/_sqlite3.cpython-312-x86_64-linux-gnu.so |

zlib version strings outside libz (string search): none found
`,ccs=` mode strings outside libc (string search): none found
JISX0213 gconv modules: ['usr/lib/x86_64-linux-gnu/gconv/EUC-JISX0213.so', 'usr/lib/x86_64-linux-gnu/gconv/SHIFT_JISX0213.so', 'usr/lib/x86_64-linux-gnu/gconv/libJISX0213.so']
nscd present: no
