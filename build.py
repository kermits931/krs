#!/usr/bin/env python3
import sys
import os
import subprocess

if os.name == 'nt':
	from colorama import init
	init()

def error(str):
	print('\n' + '\033[41m' + str + '\033[0m' + '\n')
	sys.exit(1)

def header(str):
	print('\n' + '\033[44m' + str + '\033[0m' + '\n')

# Environment checks
if not sys.version_info >= (3, 5):
	error('Requires Python >= 3.5')

if 'ANDROID_HOME' not in os.environ:
	error('Please add Android SDK path to ANDROID_HOME environment variable!')

try:
	subprocess.run(['java', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except FileNotFoundError:
	error('Please install Java and make sure \'java\' is available in PATH')

# If not Windows, we need gcc to compile
if os.name != 'nt':
	try:
		subprocess.run(['gcc', '-v'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	except FileNotFoundError:
		error('Please install C compiler and make sure \'gcc\' is available in PATH')

import argparse
import multiprocessing
import zipfile
import datetime
import errno
import shutil
import lzma
import base64
import tempfile

if 'ANDROID_NDK' in os.environ:
	ndk_build = os.path.join(os.environ['ANDROID_NDK'], 'ndk-build')
else:
	ndk_build = os.path.join(os.environ['ANDROID_HOME'], 'ndk-bundle', 'ndk-build')

def mv(source, target):
	print('mv: {} -> {}'.format(source, target))
	shutil.move(source, target)

def cp(source, target):
	print('cp: {} -> {}'.format(source, target))
	shutil.copyfile(source, target)

def rm(file):
	try:
		os.remove(file)
	except OSError as e:
		if e.errno != errno.ENOENT:
			raise

def mkdir(path, mode=0o777):
	try:
		os.mkdir(path, mode)
	except:
		pass

def mkdir_p(path, mode=0o777):
	os.makedirs(path, mode, exist_ok=True)

def zip_with_msg(zipfile, source, target):
	if not os.path.exists(source):
		error('{} does not exist! Try build \'binary\' and \'apk\' before zipping!'.format(source))
	print('zip: {} -> {}'.format(source, target))
	zipfile.write(source, target)

def build_all(args):
	build_binary(args)
	build_apk(args)
	zip_main(args)
	zip_uninstaller(args)
	build_snet(args)

def build_binary(args):
	header('* Building Magisk binaries')

	# Force update logging.h timestamp to trigger recompilation
	os.utime(os.path.join('core', 'jni', 'include', 'logging.h'))

	debug_flag = '' if args.release else '-DMAGISK_DEBUG'
	cflag = 'MAGISK_FLAGS=\"-DMAGISK_VERSION=\\\"{}\\\" -DMAGISK_VER_CODE={} {}\"'.format(args.versionString, args.versionCode, debug_flag)

	# Prebuild
	proc = subprocess.run('{} -C core PRECOMPILE=true {} -j{}'.format(ndk_build, cflag, multiprocessing.cpu_count()), shell=True)
	if proc.returncode != 0:
		error('Build Magisk binary failed!')

	print('')
	for arch in ['arm64-v8a', 'armeabi-v7a', 'x86', 'x86_64']:
		mkdir_p(os.path.join('out', arch))
		with open(os.path.join('out', arch, 'dump.h'), 'w') as dump:
			dump.write('#include "stdlib.h"\n')
			mv(os.path.join('core', 'libs', arch, 'magisk'), os.path.join('out', arch, 'magisk'))
			with open(os.path.join('out', arch, 'magisk'), 'rb') as bin:
				dump.write('const uint8_t magisk_dump[] = "')
				dump.write(''.join("\\x{:02X}".format(c) for c in lzma.compress(bin.read(), preset=9)))
				dump.write('";\n')

	print('')

	proc = subprocess.run('{} -C core {} -j{}'.format(ndk_build, cflag, multiprocessing.cpu_count()), shell=True)
	if proc.returncode != 0:
		error('Build Magisk binary failed!')

	print('')
	for arch in ['arm64-v8a', 'armeabi-v7a', 'x86', 'x86_64']:
		for binary in ['magiskinit', 'magiskboot', 'b64xz', 'busybox']:
			try:
				mv(os.path.join('core', 'libs', arch, binary), os.path.join('out', arch, binary))
			except:
				pass

def build_apk(args):
	header('* Building Magisk Manager')

	for key in ['public.certificate.x509.pem', 'private.key.pk8']:
		source = os.path.join('ziptools', key)
		target = os.path.join('app', 'src', 'main', 'assets', key)
		cp(source, target)

	for script in ['magisk_uninstaller.sh', 'util_functions.sh']:
		source = os.path.join('scripts', script)
		target = os.path.join('app', 'src', 'main', 'assets', script)
		cp(source, target)

	if args.release:
		if not os.path.exists('release_signature.jks'):
			error('Please generate a java keystore and place it in \'release_signature.jks\'')

		proc = subprocess.run('{} app:assembleRelease'.format(os.path.join('.', 'gradlew')), shell=True)
		if proc.returncode != 0:
			error('Build Magisk Manager failed!')

		unsigned = os.path.join('app', 'build', 'outputs', 'apk', 'release', 'app-release-unsigned.apk')
		aligned = os.path.join('app', 'build', 'outputs', 'apk', 'release', 'app-release-aligned.apk')
		release = os.path.join('app', 'build', 'outputs', 'apk', 'release', 'app-release.apk')

		# Find the latest build tools
		build_tool = sorted(os.listdir(os.path.join(os.environ['ANDROID_HOME'], 'build-tools')))[-1]

		rm(aligned)
		rm(release)

		proc = subprocess.run([
			os.path.join(os.environ['ANDROID_HOME'], 'build-tools', build_tool, 'zipalign'),
			'-v', '-p', '4', unsigned, aligned], stdout=subprocess.DEVNULL)
		if proc.returncode != 0:
			error('Zipalign Magisk Manager failed!')

		# Find apksigner.jar
		apksigner = ''
		for root, dirs, files in os.walk(os.path.join(os.environ['ANDROID_HOME'], 'build-tools', build_tool)):
			if 'apksigner.jar' in files:
				apksigner = os.path.join(root, 'apksigner.jar')
				break
		if not apksigner:
			error('Cannot find apksigner.jar in Android SDK build tools')

		proc = subprocess.run('java -jar {} sign --ks {} --out {} {}'.format(
			apksigner, 'release_signature.jks', release, aligned), shell=True)
		if proc.returncode != 0:
			error('Release sign Magisk Manager failed!')

		rm(unsigned)
		rm(aligned)

		mkdir('out')
		target = os.path.join('out', 'app-release.apk')
		print('')
		mv(release, target)
	else:
		proc = subprocess.run('{} app:assembleDebug'.format(os.path.join('.', 'gradlew')), shell=True)
		if proc.returncode != 0:
			error('Build Magisk Manager failed!')

		source = os.path.join('app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk')
		mkdir('out')
		target = os.path.join('out', 'app-debug.apk')
		print('')
		mv(source, target)

def build_snet(args):
	proc = subprocess.run('{} snet:assembleRelease'.format(os.path.join('.', 'gradlew')), shell=True)
	if proc.returncode != 0:
		error('Build snet extention failed!')
	source = os.path.join('snet', 'build', 'outputs', 'apk', 'release', 'snet-release-unsigned.apk')
	mkdir('out')
	target = os.path.join('out', 'snet.apk')
	print('')
	mv(source, target)

def gen_update_binary():
	update_bin = []
	binary = os.path.join('out', 'armeabi-v7a', 'b64xz')
	if not os.path.exists(binary):
		error('Please build \'binary\' before zipping!')
	with open(binary, 'rb') as b64xz:
		update_bin.append('#! /sbin/sh\nEX_ARM=\'')
		update_bin.append(''.join("\\x{:02X}".format(c) for c in b64xz.read()))
	binary = os.path.join('out', 'x86', 'b64xz')
	with open(binary, 'rb') as b64xz:
		update_bin.append('\'\nEX_X86=\'')
		update_bin.append(''.join("\\x{:02X}".format(c) for c in b64xz.read()))
	binary = os.path.join('out', 'armeabi-v7a', 'busybox')
	with open(binary, 'rb') as busybox:
		update_bin.append('\'\nBB_ARM=')
		update_bin.append(base64.b64encode(lzma.compress(busybox.read(), preset=9)).decode('ascii'))
	binary = os.path.join('out', 'x86', 'busybox')
	with open(binary, 'rb') as busybox:
		update_bin.append('\nBB_X86=')
		update_bin.append(base64.b64encode(lzma.compress(busybox.read(), preset=9)).decode('ascii'))
		update_bin.append('\n')
	with open(os.path.join('scripts', 'update_binary.sh'), 'r') as script:
		update_bin.append(script.read())
	return ''.join(update_bin)

def zip_main(args):
	header('* Packing Flashable Zip')

	unsigned = tempfile.mkstemp()[1]

	with zipfile.ZipFile(unsigned, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zipf:
		# META-INF
		# update-binary
		target = os.path.join('META-INF', 'com', 'google', 'android', 'update-binary')
		print('zip: ' + target)
		zipf.writestr(target, gen_update_binary())
		# updater-script
		source = os.path.join('scripts', 'flash_script.sh')
		target = os.path.join('META-INF', 'com', 'google', 'android', 'updater-script')
		zip_with_msg(zipf, source, target)

		# Binaries
		for lib_dir, zip_dir in [('arm64-v8a', 'arm64'), ('armeabi-v7a', 'arm'), ('x86', 'x86'), ('x86_64', 'x64')]:
			for binary in ['magiskinit', 'magiskboot']:
				source = os.path.join('out', lib_dir, binary)
				target = os.path.join(zip_dir, binary)
				zip_with_msg(zipf, source, target)

		# APK
		source = os.path.join('out', 'app-release.apk' if args.release else 'app-debug.apk')
		target = os.path.join('common', 'magisk.apk')
		zip_with_msg(zipf, source, target)

		# Scripts
		# boot_patch.sh
		source = os.path.join('scripts', 'boot_patch.sh')
		target = os.path.join('common', 'boot_patch.sh')
		zip_with_msg(zipf, source, target)
		# util_functions.sh
		source = os.path.join('scripts', 'util_functions.sh')
		with open(source, 'r') as script:
			# Add version info util_functions.sh
			util_func = script.read().replace(
				'MAGISK_VERSION_STUB', 'MAGISK_VER="{}"\nMAGISK_VER_CODE={}'.format(args.versionString, args.versionCode))
			target = os.path.join('common', 'util_functions.sh')
			print('zip: ' + source + ' -> ' + target)
			zipf.writestr(target, util_func)
		# addon.d.sh
		source = os.path.join('scripts', 'addon.d.sh')
		target = os.path.join('addon.d', '99-magisk.sh')
		zip_with_msg(zipf, source, target)

		# Prebuilts
		for chromeos in ['futility', 'kernel_data_key.vbprivk', 'kernel.keyblock']:
			source = os.path.join('chromeos', chromeos)
			zip_with_msg(zipf, source, source)

		# End of zipping

	output = os.path.join('out', 'Magisk-v{}.zip'.format(args.versionString))
	sign_adjust_zip(unsigned, output)

def zip_uninstaller(args):
	header('* Packing Uninstaller Zip')

	unsigned = tempfile.mkstemp()[1]

	with zipfile.ZipFile(unsigned, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zipf:
		# META-INF
		# update-binary
		target = os.path.join('META-INF', 'com', 'google', 'android', 'update-binary')
		print('zip: ' + target)
		zipf.writestr(target, gen_update_binary())
		# updater-script
		source = os.path.join('scripts', 'uninstaller_loader.sh')
		target = os.path.join('META-INF', 'com', 'google', 'android', 'updater-script')
		zip_with_msg(zipf, source, target)

		# Binaries
		for lib_dir, zip_dir in [('arm64-v8a', 'arm64'), ('armeabi-v7a', 'arm'), ('x86', 'x86'), ('x86_64', 'x64')]:
			source = os.path.join('out', lib_dir, 'magiskboot')
			target = os.path.join(zip_dir, 'magiskboot')
			zip_with_msg(zipf, source, target)

		source = os.path.join('scripts', 'magisk_uninstaller.sh')
		target = 'magisk_uninstaller.sh'
		zip_with_msg(zipf, source, target)

		# Scripts
		# util_functions.sh
		source = os.path.join('scripts', 'util_functions.sh')
		with open(source, 'r') as script:
			# Remove the stub
			util_func = script.read().replace(
				'MAGISK_VERSION_STUB', '')
			target = os.path.join('util_functions.sh')
			print('zip: ' + source + ' -> ' + target)
			zipf.writestr(target, util_func)

		# Prebuilts
		for chromeos in ['futility', 'kernel_data_key.vbprivk', 'kernel.keyblock']:
			source = os.path.join('chromeos', chromeos)
			zip_with_msg(zipf, source, source)

		# End of zipping

	output = os.path.join('out', 'Magisk-uninstaller-{}.zip'.format(datetime.datetime.now().strftime('%Y%m%d')))
	sign_adjust_zip(unsigned, output)

def sign_adjust_zip(unsigned, output):
	signer_name = 'zipsigner-1.1.jar'
	jarsigner = os.path.join('crypto', 'build', 'libs', signer_name)

	if os.name != 'nt' and not os.path.exists(os.path.join('ziptools', 'zipadjust')):
		header('* Building zipadjust')
		# Compile zipadjust
		proc = subprocess.run('gcc -o ziptools/zipadjust ziptools/zipadjust_src/*.c -lz', shell=True)
		if proc.returncode != 0:
			error('Build zipadjust failed!')
	if not os.path.exists(jarsigner):
		header('* Building ' + signer_name)
		proc = subprocess.run('{} crypto:shadowJar'.format(os.path.join('.', 'gradlew')), shell=True)
		if proc.returncode != 0:
			error('Build {} failed!'.format(signer_name))

	header('* Signing / Adjusting Zip')

	publicKey = os.path.join('ziptools', 'public.certificate.x509.pem')
	privateKey = os.path.join('ziptools', 'private.key.pk8')

	signed = tempfile.mkstemp()[1]

	# Unsigned->signed
	proc = subprocess.run(['java', '-jar', jarsigner,
		publicKey, privateKey, unsigned, signed])
	if proc.returncode != 0:
		error('First sign flashable zip failed!')

	adjusted = tempfile.mkstemp()[1]

	# Adjust zip
	proc = subprocess.run([os.path.join('ziptools', 'zipadjust'), signed, adjusted])
	if proc.returncode != 0:
		error('Adjust flashable zip failed!')

	# Adjusted -> output
	proc = subprocess.run(['java', '-jar', jarsigner,
		"-m", publicKey, privateKey, adjusted, output])
	if proc.returncode != 0:
		error('Second sign flashable zip failed!')

	# Cleanup
	rm(unsigned)
	rm(signed)
	rm(adjusted)

def cleanup(args):
	if len(args.target) == 0:
		args.target = ['binary', 'java', 'zip']

	if 'binary' in args.target:
		header('* Cleaning binaries')
		subprocess.run(ndk_build + ' -C core PRECOMPILE=true clean', shell=True)
		subprocess.run(ndk_build + ' -C core clean', shell=True)
		for arch in ['arm64-v8a', 'armeabi-v7a', 'x86', 'x86_64']:
			shutil.rmtree(os.path.join('out', arch), ignore_errors=True)

	if 'java' in args.target:
		header('* Cleaning java')
		subprocess.run('{} clean'.format(os.path.join('.', 'gradlew')), shell=True)
		for f in os.listdir('out'):
			if '.apk' in f:
				rm(os.path.join('out', f))

	if 'zip' in args.target:
		header('* Cleaning zip files')
		for f in os.listdir('out'):
			if '.zip' in f:
				rm(os.path.join('out', f))

parser = argparse.ArgumentParser(description='Magisk build script')
parser.add_argument('--release', action='store_true', help='compile Magisk for release')
subparsers = parser.add_subparsers(title='actions')

all_parser = subparsers.add_parser('all', help='build everything and create flashable zip with uninstaller')
all_parser.add_argument('versionString')
all_parser.add_argument('versionCode', type=int)
all_parser.set_defaults(func=build_all)

binary_parser = subparsers.add_parser('binary', help='build Magisk binaries')
binary_parser.add_argument('versionString')
binary_parser.add_argument('versionCode', type=int)
binary_parser.set_defaults(func=build_binary)

apk_parser = subparsers.add_parser('apk', help='build Magisk Manager APK')
apk_parser.set_defaults(func=build_apk)

snet_parser = subparsers.add_parser('snet', help='build snet extention for Magisk Manager')
snet_parser.set_defaults(func=build_snet)

zip_parser = subparsers.add_parser('zip', help='zip and sign Magisk into a flashable zip')
zip_parser.add_argument('versionString')
zip_parser.add_argument('versionCode', type=int)
zip_parser.set_defaults(func=zip_main)

uninstaller_parser = subparsers.add_parser('uninstaller', help='create flashable uninstaller')
uninstaller_parser.set_defaults(func=zip_uninstaller)

clean_parser = subparsers.add_parser('clean', help='clean [target...] targets: binary java zip')
clean_parser.add_argument('target', nargs='*')
clean_parser.set_defaults(func=cleanup)

if len(sys.argv) == 1:
	parser.print_help()
	sys.exit(1)

args = parser.parse_args()

args.func(args)
