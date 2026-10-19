#!/bin/bash
# Check that .c files changed in a git range have their CONFIG_ options enabled.
# Usage: scripts/check-config-for-range.sh [--fix] [-O builddir] <git-range>
# Example: scripts/check-config-for-range.sh v7.2-rc6..HEAD
# Example: scripts/check-config-for-range.sh --fix -O /tmp/build v7.2-rc6..HEAD

set -e

FIX=0
BUILD=""
while [ $# -gt 0 ]; do
	case "$1" in
		--fix) FIX=1; shift ;;
		-O) BUILD="$2"; shift 2 ;;
		*) break ;;
	esac
done

RANGE="${1:?Usage: $0 [--fix] [-O builddir] <git-range>}"

if [ -n "$BUILD" ]; then
	CONFIG="$BUILD/.config"
	MAKE_OPTS="O=$BUILD"
else
	CONFIG=".config"
	MAKE_OPTS=""
fi

if [ ! -f "$CONFIG" ]; then
	echo "ERROR: config file '$CONFIG' not found" >&2
	exit 1
fi

missing=0
skipped=0
ok=0
missing_configs=""

for cfile in $(git diff --name-only "$RANGE" -- '*.c'); do
	# skip tools/ - userspace, no CONFIG_ options
	case "$cfile" in
		tools/*) continue ;;
	esac

	dir=$(dirname "$cfile")
	base=$(basename "$cfile" .c)
	obj="${base}.o"

	# find the Makefile governing this file
	makefile=""
	for mf in "$dir/Makefile" "$dir/Kbuild"; do
		if [ -f "$mf" ]; then
			makefile="$mf"
			break
		fi
	done

	if [ -z "$makefile" ]; then
		# try parent dir (for files referenced as subdir/file.o)
		pdir=$(dirname "$dir")
		subdir=$(basename "$dir")
		for mf in "$pdir/Makefile" "$pdir/Kbuild"; do
			if [ -f "$mf" ]; then
				makefile="$mf"
				obj="${subdir}/${obj}"
				dir="$pdir"
				break
			fi
		done
	fi

	if [ -z "$makefile" ]; then
		echo "SKIP $cfile  (no Makefile found)"
		skipped=$((skipped + 1))
		continue
	fi

	# Step 1: check if obj is directly in an obj-$(CONFIG_*) line
	config=$(grep -E "obj-\\$\(CONFIG_[A-Za-z0-9_]+\).*[[:space:]+=]${obj}([[:space:]]|$|\\\\)" "$makefile" \
		| grep -oE 'CONFIG_[A-Za-z0-9_]+' | head -1)

	# Step 2: if not found directly, the .o might be part of a composite module
	# Look for module-y or module-objs lines that include this .o
	if [ -z "$config" ]; then
		# find which module includes this object
		mod=$(grep -E "^[a-zA-Z0-9_]+-[yY[:space:]].*${obj}" "$makefile" \
			| sed -E 's/^([a-zA-Z0-9_]+)-.*/\1/' | head -1)

		# also check module-objs and module-$(CONFIG_*) patterns
		if [ -z "$mod" ]; then
			mod=$(grep -E "^[a-zA-Z0-9_]+-objs.*${obj}" "$makefile" \
				| sed -E 's/^([a-zA-Z0-9_]+)-.*/\1/' | head -1)
		fi
		if [ -z "$mod" ]; then
			mod=$(grep -E "^[a-zA-Z0-9_]+-\\\$\(CONFIG_.*${obj}" "$makefile" \
				| sed -E 's/^([a-zA-Z0-9_]+)-.*/\1/' | head -1)
		fi

		if [ -n "$mod" ]; then
			# now find obj-$(CONFIG_*) += mod.o
			config=$(grep -E "obj-\\$\(CONFIG_[A-Za-z0-9_]+\).*[[:space:]+=]${mod}\\.o([[:space:]]|$|\\\\)" "$makefile" \
				| grep -oE 'CONFIG_[A-Za-z0-9_]+' | head -1)
			# the module might also be pulled in via a subdir from parent
			if [ -z "$config" ]; then
				pdir=$(dirname "$dir")
				for pmf in "$pdir/Makefile" "$pdir/Kbuild"; do
					if [ -f "$pmf" ]; then
						config=$(grep -E "obj-\\$\(CONFIG_[A-Za-z0-9_]+\).*[[:space:]+=]${mod}\\.o([[:space:]]|$|\\\\)" "$pmf" \
							| grep -oE 'CONFIG_[A-Za-z0-9_]+' | head -1)
						[ -n "$config" ] && break
					fi
				done
			fi
		fi
	fi

	# Step 3: check if it's always compiled (obj-y)
	# Use sed to join backslash-continued lines, then search
	if [ -z "$config" ]; then
		joined=$(sed -e ':a' -e '/\\$/{N;s/\\\n//;ba}' "$makefile")
		if echo "$joined" | grep -qE "obj-y.*[[:space:]+=]${obj}([[:space:]]|$)"; then
			echo "OK   $cfile  (always compiled, obj-y)"
			ok=$((ok + 1))
			continue
		fi
		if echo "$joined" | grep -qE "(lib|core|common)-y.*[[:space:]+=]${obj}([[:space:]]|$)"; then
			echo "OK   $cfile  (always compiled)"
			ok=$((ok + 1))
			continue
		fi
		# check if part of a module that's obj-y
		if [ -n "$mod" ]; then
			if echo "$joined" | grep -qE "obj-y.*[[:space:]+=]${mod}\\.o([[:space:]]|$)"; then
				echo "OK   $cfile  (always compiled via ${mod}, obj-y)"
				ok=$((ok + 1))
				continue
			fi
		fi
	fi

	# Step 4: for subdirectory-based builds, check parent Makefile
	# e.g. drivers/vdpa/alibaba/eni_vdpa.c - parent has obj-$(CONFIG_X) += alibaba/
	if [ -z "$config" ]; then
		subdir_name=$(basename "$dir")
		pdir=$(dirname "$dir")
		for pmf in "$pdir/Makefile" "$pdir/Kbuild"; do
			if [ -f "$pmf" ]; then
				config=$(grep -E "obj-\\$\(CONFIG_[A-Za-z0-9_]+\).*[[:space:]+=]${subdir_name}/" "$pmf" \
					| grep -oE 'CONFIG_[A-Za-z0-9_]+' | head -1)
				[ -n "$config" ] && break
			fi
		done
	fi

	if [ -z "$config" ]; then
		echo "SKIP $cfile  (could not determine CONFIG_ option)"
		skipped=$((skipped + 1))
		continue
	fi

	# check if config is enabled
	if grep -qE "^${config}=[ym]" "$CONFIG"; then
		echo "OK   $cfile  ($config is enabled)"
		ok=$((ok + 1))
	else
		echo "MISS $cfile  ($config is NOT enabled)"
		missing=$((missing + 1))
		missing_configs="$missing_configs $config"
	fi
done

echo ""
echo "Summary: $ok ok, $missing missing, $skipped skipped"
if [ "$missing" -gt 0 ]; then
	# deduplicate
	missing_configs=$(echo "$missing_configs" | tr ' ' '\n' | sort -u | tr '\n' ' ')
	if [ "$FIX" -eq 1 ]; then
		frag=$(mktemp /tmp/kconfig-fragment.XXXXXX)
		for cfg in $missing_configs; do
			echo "${cfg}=y" >> "$frag"
		done
		echo "Merging config fragment:"
		cat "$frag"
		scripts/kconfig/merge_config.sh -m ${BUILD:+-O "$BUILD"} "$CONFIG" "$frag"
		make $MAKE_OPTS olddefconfig
		rm -f "$frag"
		# report any that still didn't make it
		still_missing=""
		for cfg in $missing_configs; do
			if ! grep -qE "^${cfg}=[ym]" "$CONFIG"; then
				still_missing="$still_missing $cfg"
			fi
		done
		if [ -n "$still_missing" ]; then
			echo ""
			echo "Could not enable (unmet dependencies):$still_missing"
			echo "Check their 'depends on' in Kconfig and enable deps manually."
		fi
	else
		echo "To fix, run: $0 --fix ${BUILD:+-O $BUILD }$RANGE"
		exit 1
	fi
elif [ "$FIX" -eq 1 ]; then
	make $MAKE_OPTS olddefconfig
	echo "Done. Re-run without --fix to verify."
fi

if [ "$missing" -gt 0 ]; then
	exit 1
fi
