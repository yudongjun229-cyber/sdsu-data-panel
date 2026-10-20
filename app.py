import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, abort, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from config import Config
from models import db, User, Dataset, Keyword, DownloadLog, StorageConfig, VariableDefinition, SiteLocation
from services.data_parser import parse_dataset_visualization_data
from services.stats_service import get_dashboard_summary
from services.oss_service import upload_to_oss, get_oss_download_url, get_active_oss_config
from services.i18n import TRANSLATIONS

app = Flask(__name__)
app.config.from_object(Config)

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to access this feature.'
login_manager.login_message_category = 'info'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ----------------- Pure English Localization Context -----------------
@app.context_processor
def inject_i18n():
    current_lang = 'en'
    
    def t(key, *args):
        lang_dict = TRANSLATIONS.get('en', {})
        val = lang_dict.get(key, key)
        if args:
            try:
                return val.format(*args)
            except Exception:
                return val
        return val

    return {
        'current_lang': current_lang,
        't': t
    }

@app.route('/set-language/<lang>')
def set_language(lang):
    next_page = request.referrer or url_for('index')
    return redirect(next_page)

# ----------------- Database Initialization and Seed Data -----------------
def seed_database():
    with app.app_context():
        db.create_all()

        # 1. Ensure administrator and default researcher accounts
        admin_user = User.query.filter_by(username='admin').first()
        if not admin_user:
            admin = User(
                username='admin',
                email='admin@sdsu.edu',
                role='admin',
                institution='SDSU EMAIL Lab'
            )
            admin.set_password('admin123')
            db.session.add(admin)

        researcher = User.query.filter_by(username='researcher').first()
        if not researcher:
            res = User(
                username='researcher',
                email='researcher@cas.ac.cn',
                role='user',
                institution='Institute of Geographic Sciences and Natural Resources Research, CAS'
            )
            res.set_password('res123')
            db.session.add(res)

        db.session.commit()

        # 2. Seed taxonomy keywords (aligned with MicrobialDataSystem)
        default_keywords = [
            'soil_microbial_biomass',
            'microbial_biomass_c',
            'microbial_biomass_n',
            'microbial_biomass_p',
            'fungal_biomass',
            'bacterial_biomass',
            'fungal_bacterial_ratio',
            'soil_doc',
            'soil_respiration',
            'metabolic_quotient',
            'microbial_stoichiometry',
            'arctic_tundra',
            'peatland_wetland',
            'forest_soil',
            'soil_ph'
        ]
        kw_map = {}
        for kw_name in default_keywords:
            kw_obj = Keyword.query.filter_by(name=kw_name).first()
            if not kw_obj:
                kw_obj = Keyword(name=kw_name)
                db.session.add(kw_obj)
                db.session.flush()
            kw_map[kw_name] = kw_obj
        db.session.commit()

        # 2.1 Standardized Scientific Variables (MicrobialDataSystem & Soil Ecology)
        default_variables = [
            # Microbial Biomass & Stoichiometry
            ('MBC', 'Soil Microbial Biomass Carbon', 'Soil Microbial Biomass Carbon', 'Microbial Pool', 'mmol kg-1', 'Total living microbial biomass carbon in soil (Chloroform fumigation extraction)', 'Total living microbial biomass carbon in soil'),
            ('MBN', 'Soil Microbial Biomass Nitrogen', 'Soil Microbial Biomass Nitrogen', 'Microbial Pool', 'mmol kg-1', 'Total living microbial biomass nitrogen in soil', 'Total living microbial biomass nitrogen in soil'),
            ('MBP', 'Soil Microbial Biomass Phosphorus', 'Soil Microbial Biomass Phosphorus', 'Microbial Pool', 'mmol kg-1', 'Total living microbial biomass phosphorus in soil', 'Total living microbial biomass phosphorus in soil'),
            ('FBC', 'Fungal Biomass Carbon', 'Fungal Biomass Carbon', 'Microbial Pool', 'g C m-2', 'Explicit active fungal mycelial carbon pool', 'Explicit active fungal mycelial carbon pool'),
            ('BBC', 'Bacterial Biomass Carbon', 'Bacterial Biomass Carbon', 'Microbial Pool', 'g C m-2', 'Explicit active bacterial carbon pool', 'Explicit active bacterial carbon pool'),
            ('Q_MET', 'Microbial Metabolic Quotient (qCO2)', 'Microbial Metabolic Quotient (qCO2)', 'Microbial Pool', 'mg C g-1 MBC h-1', 'Specific soil respiration rate per unit microbial biomass', 'Specific soil respiration rate per unit microbial biomass'),

            # Soil Chemistry & Physical Properties
            ('SOC', 'Soil Organic Carbon', 'Soil Organic Carbon', 'Soil Physics', 'mmol kg-1', 'Total organic carbon content in soil matrix', 'Total organic carbon content in soil matrix'),
            ('TN', 'Total Soil Nitrogen', 'Total Soil Nitrogen', 'Soil Physics', 'mmol kg-1', 'Total nitrogen content in soil matrix', 'Total nitrogen content in soil matrix'),
            ('TOP', 'Total Organic Phosphorus', 'Total Organic Phosphorus', 'Soil Physics', 'mmol kg-1', 'Total organic phosphorus pool in soil', 'Total organic phosphorus pool in soil'),
            ('DOC', 'Soil Dissolved Organic Carbon', 'Soil Dissolved Organic Carbon', 'Soil Physics', 'mg C kg-1', 'Dissolved organic carbon readily bioavailable to microbes', 'Dissolved organic carbon readily bioavailable to microbes'),
            ('PH', 'Soil pH', 'Soil pH', 'Soil Physics', 'pH units', 'Soil reaction in water/CaCl2 suspension', 'Soil reaction in water/CaCl2 suspension'),
            ('MAT', 'Mean Annual Temperature', 'Mean Annual Temperature', 'Soil Physics', '°C', 'Long-term mean annual air temperature', 'Long-term mean annual air temperature'),
            ('MAP', 'Mean Annual Precipitation', 'Mean Annual Precipitation', 'Soil Physics', 'mm', 'Long-term mean annual precipitation sum', 'Long-term mean annual precipitation sum'),
            ('TSOI', 'Soil Profile Temperature', 'Soil Profile Temperature', 'Soil Physics', '°C', 'Layered soil profile thermal temperature', 'Layered soil profile thermal temperature'),

            # Gas Fluxes
            ('HR', 'Heterotrophic Respiration', 'Heterotrophic Respiration', 'Gas Flux', 'g C m-2 d-1', 'Microbial heterotrophic decomposition carbon flux', 'Microbial heterotrophic decomposition carbon flux'),
            ('CH4_FLUX', 'Net Methane Surface Flux', 'Net Methane Surface Flux', 'Gas Flux', 'mg C m-2 d-1', 'Net surface methane exchange flux', 'Net surface methane exchange flux')
        ]
        var_map = {}
        for code, dname, dname_en, cat, unit, desc, desc_en in default_variables:
            var_obj = VariableDefinition.query.filter_by(code=code).first()
            if not var_obj:
                var_obj = VariableDefinition(
                    code=code,
                    display_name=dname,
                    display_name_en=dname_en,
                    category=cat,
                    unit=unit,
                    description=desc,
                    description_en=desc_en
                )
                db.session.add(var_obj)
                db.session.flush()
            else:
                var_obj.display_name = dname
                var_obj.display_name_en = dname_en
                var_obj.category = cat
                var_obj.unit = unit
                var_obj.description = desc
                var_obj.description_en = desc_en
            var_map[code] = var_obj
        db.session.commit()

        # 2.2 Global Ecological Sites
        default_sites = [
            ('Global-Terrestrial', 'Global Terrestrial Ecosystems (Xu et al. 2013)', 'Global', 20.0, 0.0, 300.0, 'Global / Multi-Biome'),
            ('US-Atq', 'Atqasuk Tundra Research Site', 'United States', 70.47, -157.41, 15.0, 'Tundra'),
            ('US-Brw', 'Barrow Environmental Observatory', 'United States', 71.32, -156.61, 4.0, 'Tundra'),
            ('US-SPR', 'SPRUCE Peatland (Peatland Heating Experiment)', 'United States', 47.50, -93.45, 418.0, 'Peatland/Boreal Bog'),
            ('CN-Sanjiang', 'Sanjiang Mire Wetland Research Station', 'China', 47.58, 133.52, 55.0, 'Wetland'),
            ('SE-St1', 'Stordalen Mire (Subarctic Permafrost Peatland)', 'Sweden', 68.35, 19.05, 360.0, 'Peatland/Permafrost')
        ]
        site_map = {}
        for scode, sname, country, lat, lon, elev, biome in default_sites:
            s_obj = SiteLocation.query.filter_by(site_code=scode).first()
            if not s_obj:
                s_obj = SiteLocation(
                    site_code=scode,
                    site_name=sname,
                    country=country,
                    latitude=lat,
                    longitude=lon,
                    elevation=elev,
                    biome=biome
                )
                db.session.add(s_obj)
                db.session.flush()
            site_map[scode] = s_obj
        db.session.commit()

        # 3. Uploads directory setup
        data_dir = os.path.join(app.config['UPLOAD_FOLDER'])
        os.makedirs(data_dir, exist_ok=True)

        # 4. Storage configuration sample
        if StorageConfig.query.count() == 0:
            default_oss = StorageConfig(
                provider='aliyun_oss',
                endpoint='oss-cn-beijing.aliyuncs.com',
                bucket_name='sdsu-microbial-data-bucket',
                access_key_id='LTAI5tDemoAccessKeyId2026',
                access_key_secret='DemoSecretAccessKeySecret987654321',
                custom_domain='https://data-cdn.sdsu-microbe.org',
                is_active=True
            )
            db.session.add(default_oss)
            db.session.commit()

        # 5. Seed Demonstration Datasets (MicrobialDataSystem - 2 Real Physical Datasets)
        if Dataset.query.count() == 0:
            from datetime import date
            
            # Dataset 1: Soil_Microbial_Biomass_C_N_P_spatial.csv
            ds1 = Dataset(
                title='Global Terrestrial Soil Microbial Biomass C/N/P In-situ Spatial Observation Dataset',
                year=2020,
                author='Xiaofeng Xu, P. E. Thornton, W. M. Post, et al. (SDSU, CAS, Minzu Univ & ORNL)',
                doi='10.1111/geb.12029',
                description='Physical file: Soil_Microbial_Biomass_C_N_P_spatial.csv archived in email-clm/MicrobialDataSystem. Synthesizes 3,400+ in-situ empirical observations across 50 countries, covering global terrestrial biomes. Measures soil microbial biomass carbon (MBC), nitrogen (MBN), phosphorus (MBP), soil organic carbon (SOC), total nitrogen (TN), total organic phosphorus (TOP), pH, MAT, and MAP along depth profiles. References: Xu et al. (2013 GEB), Xu et al. (2015 Sci Rep), Xu et al. (2017 Ecol Monogr), He et al. (2020 SBB), and Guo et al. (2020 GEB).',
                data_source_type='github',
                github_url='https://raw.githubusercontent.com/email-clm/MicrobialDataSystem/master/Soil_Microbial_Biomass_C_N_P_spatial.csv',
                local_file_path='Soil_Microbial_Biomass_C_N_P_spatial.csv',
                download_count=2,
                site_code='Global-Terrestrial',
                site=site_map.get('Global-Terrestrial'),
                spatial_scale='Global',
                start_date=date(1980, 1, 1),
                end_date=date(2020, 12, 31),
                temporal_resolution='Yearly',
                data_type='Field Observation',
                scenario='Control',
                min_depth=0.0,
                max_depth=1.0
            )
            ds1.keywords.extend([
                kw_map['soil_microbial_biomass'],
                kw_map['microbial_biomass_c'],
                kw_map['microbial_biomass_n'],
                kw_map['microbial_biomass_p'],
                kw_map['microbial_stoichiometry'],
                kw_map['soil_ph']
            ])
            ds1.variables.extend([
                var_map['MBC'],
                var_map['MBN'],
                var_map['MBP'],
                var_map['SOC'],
                var_map['TN'],
                var_map['TOP'],
                var_map['PH'],
                var_map['MAT'],
                var_map['MAP']
            ])

            # Dataset 2: Global_Soil_Microbial_BiomassCN.nc
            ds2 = Dataset(
                title='Global Soil Microbial Biomass Carbon & Nitrogen 0.5-Degree Gridded Simulation Dataset',
                year=2021,
                author='Xiaofeng Xu, D. Hui, A. W. King, et al. (SDSU & CAS)',
                doi='10.1038/srep17445',
                description='Physical file: Global_Soil_Microbial_BiomassCN.nc (NetCDF) archived in email-clm/MicrobialDataSystem. Delivers continuous 0.5° × 0.5° gridded simulations of global topsoil and subsoil microbial biomass carbon and nitrogen distribution, stoichiometric convergence, and microbial assimilation of carbon, nitrogen, phosphorus, and sulfur across global terrestrial ecosystems.',
                data_source_type='github',
                github_url='https://raw.githubusercontent.com/email-clm/MicrobialDataSystem/master/Global_Soil_Microbial_BiomassCN.nc',
                local_file_path='Global_Soil_Microbial_BiomassCN.nc',
                download_count=1,
                site_code='Global-Terrestrial',
                site=site_map.get('Global-Terrestrial'),
                spatial_scale='Global',
                start_date=date(2000, 1, 1),
                end_date=date(2021, 12, 31),
                temporal_resolution='Monthly',
                data_type='Model Simulation',
                scenario='Control',
                min_depth=0.0,
                max_depth=1.5
            )
            ds2.keywords.extend([
                kw_map['soil_microbial_biomass'],
                kw_map['microbial_biomass_c'],
                kw_map['microbial_biomass_n'],
                kw_map['microbial_stoichiometry'],
                kw_map['fungal_biomass'],
                kw_map['bacterial_biomass']
            ])
            ds2.variables.extend([
                var_map['MBC'],
                var_map['MBN'],
                var_map['FBC'],
                var_map['BBC'],
                var_map['SOC'],
                var_map['TN'],
                var_map['MAT'],
                var_map['MAP']
            ])

            db.session.add_all([ds1, ds2])
            db.session.commit()

            # Seed Audit Logs
            log1 = DownloadLog(
                user_id=admin_user.id if admin_user else 1,
                dataset_id=ds1.id,
                download_type='github_redirect',
                ip_address='127.0.0.1',
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X)',
                institution='SDSU Department of Biology',
                purpose='Model Calibration & Validation'
            )
            log2 = DownloadLog(
                user_id=researcher.id if researcher else 2,
                dataset_id=ds2.id,
                download_type='github_redirect',
                ip_address='127.0.0.1',
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X)',
                institution='Northeast Institute of Geography and Agroecology, CAS',
                purpose='Academic Research'
            )
            log3 = DownloadLog(
                user_id=None,
                dataset_id=ds1.id,
                download_type='github_redirect',
                ip_address='192.168.1.102',
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                institution='Oak Ridge National Laboratory',
                purpose='Teaching & Education'
            )
            db.session.add_all([log1, log2, log3])
            db.session.commit()

# ----------------- Application Routes -----------------

@app.route('/')
def index():
    # 1. Full-text search
    query_str = request.args.get('q', '').strip()

    # 2. Temporal coverage slicing
    start_year = request.args.get('start_year', type=int)
    end_year = request.args.get('end_year', type=int)
    temp_res = request.args.get('temp_res', '').strip()

    # 3. Spatial and Biome dimension
    selected_biomes = request.args.getlist('biome')
    selected_site = request.args.get('site', '').strip()
    spatial_scale = request.args.get('spatial_scale', '').strip()
    min_lat = request.args.get('min_lat', type=float)
    max_lat = request.args.get('max_lat', type=float)

    # 4. Standardized scientific variables checklist
    selected_vars = request.args.getlist('var')

    # 5. Experimental context & data types
    data_type = request.args.get('data_type', '').strip()
    scenario = request.args.get('scenario', '').strip()

    # 6. Soil depth range (m)
    depth_max_filter = request.args.get('depth_max', type=float)

    # 7. Taxonomy tag
    keyword_str = request.args.get('keyword', '').strip()

    # Build SQLAlchemy dynamic query
    query = Dataset.query.join(Dataset.site, isouter=True)

    if query_str:
        # Multi-field combined fuzzy search
        query = query.filter(
            (Dataset.title.ilike(f'%{query_str}%')) | 
            (Dataset.description.ilike(f'%{query_str}%')) |
            (Dataset.author.ilike(f'%{query_str}%')) |
            (SiteLocation.site_name.ilike(f'%{query_str}%')) |
            (SiteLocation.site_code.ilike(f'%{query_str}%')) |
            (Dataset.variables.any(VariableDefinition.code.ilike(f'%{query_str}%'))) |
            (Dataset.variables.any(VariableDefinition.display_name.ilike(f'%{query_str}%'))) |
            (Dataset.variables.any(VariableDefinition.display_name_en.ilike(f'%{query_str}%'))) |
            (Dataset.keywords.any(Keyword.name.ilike(f'%{query_str}%')))
        )

    # Temporal range filtering
    if start_year:
        query = query.filter(
            (db.func.strftime('%Y', Dataset.end_date) >= str(start_year)) |
            (Dataset.year >= start_year)
        )
    if end_year:
        query = query.filter(
            (db.func.strftime('%Y', Dataset.start_date) <= str(end_year)) |
            (Dataset.year <= end_year)
        )
    if temp_res:
        query = query.filter(Dataset.temporal_resolution == temp_res)

    # Biome & site
    if selected_biomes:
        query = query.filter(SiteLocation.biome.in_(selected_biomes))
    if selected_site:
        query = query.filter((SiteLocation.site_code == selected_site) | (Dataset.site_code == selected_site))
    if spatial_scale:
        query = query.filter(Dataset.spatial_scale == spatial_scale)
    if min_lat is not None and max_lat is not None:
        query = query.filter(SiteLocation.latitude.between(min_lat, max_lat))

    # Variable intersection filtering
    if selected_vars:
        for v_code in selected_vars:
            query = query.filter(Dataset.variables.any(VariableDefinition.code == v_code))

    # Data type & scenario
    if data_type:
        query = query.filter(Dataset.data_type == data_type)
    if scenario:
        query = query.filter(Dataset.scenario == scenario)

    # Soil depth filtering
    if depth_max_filter is not None:
        query = query.filter(Dataset.max_depth <= depth_max_filter)

    # Keyword tag
    if keyword_str:
        query = query.filter(Dataset.keywords.any(Keyword.name == keyword_str))

    datasets = query.order_by(Dataset.created_at.desc()).all()
    keywords = Keyword.query.order_by(Keyword.name.asc()).all()

    # Slicer dimension options
    all_variables = VariableDefinition.query.order_by(VariableDefinition.category.asc(), VariableDefinition.code.asc()).all()
    all_biomes = [r[0] for r in db.session.query(SiteLocation.biome).distinct().filter(SiteLocation.biome != None).all()]
    all_sites = SiteLocation.query.order_by(SiteLocation.site_code.asc()).all()

    total_downloads = db.session.query(db.func.sum(Dataset.download_count)).scalar() or 0

    return render_template(
        'index.html',
        datasets=datasets,
        keywords=keywords,
        all_variables=all_variables,
        all_biomes=all_biomes,
        all_sites=all_sites,
        query=query_str,
        start_year=start_year,
        end_year=end_year,
        selected_biomes=selected_biomes,
        selected_site=selected_site,
        selected_vars=selected_vars,
        selected_data_type=data_type,
        selected_scenario=scenario,
        selected_kw=keyword_str,
        total_downloads=total_downloads
    )

@app.route('/dataset/<int:dataset_id>')
def detail(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    
    # Read attached NetCDF or CSV file for real scientific visualization telemetry
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], dataset.local_file_path or 'Soil_Microbial_Biomass_C_N_P_spatial.csv')
    vis_data = parse_dataset_visualization_data(file_path)

    return render_template(
        'detail.html',
        dataset=dataset,
        vis_data=vis_data
    )

@app.route('/analysis')
def analysis():
    flash('Interactive model diagnostics module is undergoing maintenance and upgrades.', 'info')
    return redirect(url_for('index'))

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        author = request.form.get('author', '').strip()
        year = request.form.get('year', '').strip()
        site_code = request.form.get('site_code', '').strip()
        site_id = request.form.get('site_id', type=int)
        spatial_scale = request.form.get('spatial_scale', 'Point')
        start_date_str = request.form.get('start_date', '').strip()
        end_date_str = request.form.get('end_date', '').strip()
        temporal_resolution = request.form.get('temporal_resolution', 'Daily')
        data_type = request.form.get('data_type', 'Field Observation')
        scenario = request.form.get('scenario', 'Control')
        min_depth = request.form.get('min_depth', type=float) or 0.0
        max_depth = request.form.get('max_depth', type=float) or 1.0
        doi = request.form.get('doi', '').strip()
        description = request.form.get('description', '').strip()
        data_source_type = request.form.get('data_source_type', 'github')
        github_url = request.form.get('github_url', '').strip()
        keywords_str = request.form.get('keywords', '').strip()
        var_ids = request.form.getlist('variables')

        if not title or not author or not description:
            flash('Please fill in dataset title, lead author, and scientific abstract.', 'error')
            return redirect(url_for('upload'))

        # Parse temporal range dates
        from datetime import date
        s_date = None
        e_date = None
        try:
            if start_date_str:
                s_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            if end_date_str:
                e_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except Exception:
            pass

        yr_val = int(year) if year.isdigit() else (s_date.year if s_date else 2023)
        if not s_date:
            s_date = date(yr_val, 1, 1)
        if not e_date:
            e_date = date(yr_val, 12, 31)

        # Handle file source: GitHub, Local, or OSS
        local_filename = None
        if data_source_type == 'oss':
            file = request.files.get('oss_file') or request.files.get('file')
            if file and file.filename:
                fname = secure_filename(file.filename)
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                file.save(save_path)
                local_filename = fname
                oss_key = f"datasets/{yr_val}/{fname}"
                upload_to_oss(save_path, oss_key)
            else:
                fname = f"sample_oss_{yr_val}.csv"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                generate_sample_clm_timeseries(save_path, year=yr_val)
                local_filename = fname
                upload_to_oss(save_path, f"datasets/{yr_val}/{fname}")
        elif data_source_type == 'local':
            file = request.files.get('file')
            if file and file.filename:
                fname = secure_filename(file.filename)
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                file.save(save_path)
                local_filename = fname
            else:
                local_filename = 'Soil_Microbial_Biomass_C_N_P_spatial.csv'
        else:
            local_filename = 'Soil_Microbial_Biomass_C_N_P_spatial.csv'

        target_site = None
        if site_id:
            target_site = db.session.get(SiteLocation, site_id)
        elif site_code:
            target_site = SiteLocation.query.filter_by(site_code=site_code).first()

        new_dataset = Dataset(
            title=title,
            author=author,
            year=yr_val,
            site=target_site,
            site_code=target_site.site_code if target_site else (site_code or 'US-Atq'),
            spatial_scale=spatial_scale,
            start_date=s_date,
            end_date=e_date,
            temporal_resolution=temporal_resolution,
            data_type=data_type,
            scenario=scenario,
            min_depth=min_depth,
            max_depth=max_depth,
            doi=doi,
            description=description,
            data_source_type=data_source_type,
            github_url=github_url if data_source_type == 'github' else None,
            local_file_path=local_filename,
            download_count=0
        )
        db.session.add(new_dataset)
        db.session.flush()

        # Relationships: variables (existing + dynamic custom)
        if var_ids:
            for vid in var_ids:
                if str(vid).isdigit():
                    v_obj = db.session.get(VariableDefinition, int(vid))
                    if v_obj and v_obj not in new_dataset.variables:
                        new_dataset.variables.append(v_obj)

        new_custom_vars = request.form.get('custom_variables', '').strip()
        if new_custom_vars:
            # Format: "CODE:UNIT" or "CODE", comma-separated
            raw_v_items = [v.strip() for v in new_custom_vars.replace(',', ' ').split() if v.strip()]
            for item in raw_v_items:
                parts = item.split(':')
                v_code = parts[0].strip().upper()
                v_unit = parts[1].strip() if len(parts) > 1 else 'dimensionless'
                v_obj = VariableDefinition.query.filter_by(code=v_code).first()
                if not v_obj:
                    v_obj = VariableDefinition(
                        code=v_code,
                        display_name=f"{v_code} (User Added)",
                        display_name_en=f"{v_code} (User Added)",
                        category='Microbial Pool',
                        unit=v_unit,
                        description='User-defined microbial variable on upload',
                        description_en='User-defined microbial variable on upload'
                    )
                    db.session.add(v_obj)
                    db.session.flush()
                if v_obj not in new_dataset.variables:
                    new_dataset.variables.append(v_obj)

        # Relationships: keywords (existing checked + dynamic custom)
        selected_kw_ids = request.form.getlist('keyword_ids')
        if selected_kw_ids:
            for kid in selected_kw_ids:
                if str(kid).isdigit():
                    k_obj = db.session.get(Keyword, int(kid))
                    if k_obj and k_obj not in new_dataset.keywords:
                        new_dataset.keywords.append(k_obj)

        if keywords_str:
            raw_kws = [k.strip().lower() for k in keywords_str.replace(',', ' ').split() if k.strip()]
            for k_name in raw_kws:
                kw_obj = Keyword.query.filter_by(name=k_name).first()
                if not kw_obj:
                    kw_obj = Keyword(name=k_name)
                    db.session.add(kw_obj)
                    db.session.flush()
                if kw_obj not in new_dataset.keywords:
                    new_dataset.keywords.append(kw_obj)

        db.session.commit()
        flash('Scientific dataset submitted successfully with multi-dimensional variables and spatio-temporal properties.', 'success')
        return redirect(url_for('detail', dataset_id=new_dataset.id))

    all_sites = SiteLocation.query.order_by(SiteLocation.site_code.asc()).all()
    all_variables = VariableDefinition.query.order_by(VariableDefinition.category.asc()).all()
    all_keywords = Keyword.query.order_by(Keyword.name.asc()).all()
    return render_template('upload.html', all_sites=all_sites, all_variables=all_variables, all_keywords=all_keywords)

# ----------------- Usage Audit & Download Dispatch Route -----------------
@app.route('/dataset/<int:dataset_id>/download', methods=['POST'])
def trigger_download(dataset_id):
    dataset = Dataset.query.get_or_404(dataset_id)
    
    # 1. Retrieve telemetry audit payload from client
    data = request.get_json() or {}
    institution = data.get('institution', 'Unspecified Institution')
    purpose = data.get('purpose', 'Academic Research')

    # Record audit log
    dl_type = 'github_redirect'
    if dataset.data_source_type == 'oss':
        dl_type = 'oss_download'
    elif dataset.data_source_type == 'local':
        dl_type = 'local_download'

    log = DownloadLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        dataset_id=dataset.id,
        download_type=dl_type,
        ip_address=request.remote_addr,
        user_agent=request.user_agent.string[:250] if request.user_agent else '',
        institution=institution,
        purpose=purpose
    )
    dataset.download_count += 1
    db.session.add(log)
    db.session.commit()

    # Dispatch: GitHub repository / direct file, OSS presigned/CDN, or local stream
    if dataset.data_source_type == 'github':
        target_gh_url = dataset.github_url or 'https://github.com/email-clm/MicrobialDataSystem'
        return jsonify({'status': 'success', 'download_url': target_gh_url})
    elif dataset.data_source_type == 'oss':
        oss_key = f"datasets/{dataset.year}/{dataset.local_file_path or 'data.csv'}"
        oss_url = get_oss_download_url(oss_key)
        return jsonify({'status': 'success', 'download_url': oss_url or url_for('download_file', filename=dataset.local_file_path)})
    elif dataset.local_file_path:
        return jsonify({'status': 'success', 'download_url': url_for('download_file', filename=dataset.local_file_path)})
    else:
        return jsonify({'status': 'error', 'message': 'Requested data file not found on server.'}), 404

@app.route('/files/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

# ----------------- User Authentication -----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if action == 'register':
            confirm_password = request.form.get('confirm_password', '').strip()
            email = request.form.get('email', '').strip()
            institution = request.form.get('institution', '').strip()

            if not username or not password or not email or not confirm_password:
                flash('Please complete all required registration fields.', 'error')
                return redirect(url_for('login'))

            if password != confirm_password:
                flash('Passwords do not match. Please re-enter.', 'error')
                return redirect(url_for('login'))

            if len(password) < 6:
                flash('Password must contain at least 6 characters.', 'error')
                return redirect(url_for('login'))

            if User.query.filter_by(username=username).first():
                flash('Username already taken. Please choose another.', 'error')
                return redirect(url_for('login'))
            
            user = User(username=username, email=email, institution=institution, role='user')
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Registration successful! You are now signed in.', 'success')
            return redirect(url_for('index'))
        else:
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                flash(f'Welcome back, {user.username}!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('index'))
            flash('Invalid username or password.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have safely signed out.', 'info')
    return redirect(url_for('index'))

# ----------------- Admin Dashboard -----------------
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Access denied. Administrator privileges required.', 'error')
        return redirect(url_for('index'))

    summary = get_dashboard_summary()
    return render_template('admin/dashboard.html', summary=summary)

# ----------------- Data-Driven Keywords Governance -----------------
@app.route('/dataset/<int:dataset_id>/keywords', methods=['POST'])
@login_required
def manage_dataset_keyword(dataset_id):
    if current_user.role != 'admin':
        flash('Access denied. Administrator privileges required.', 'error')
        return redirect(url_for('detail', dataset_id=dataset_id))

    dataset = Dataset.query.get_or_404(dataset_id)
    action = request.form.get('action')
    redirect_to = request.form.get('redirect_to', 'detail')

    if action == 'add':
        existing_kw_id = request.form.get('existing_keyword_id')
        new_kw_name = request.form.get('keyword_name', '').strip().lower()

        target_kw = None
        if existing_kw_id and existing_kw_id.isdigit():
            target_kw = db.session.get(Keyword, int(existing_kw_id))
        elif new_kw_name:
            target_kw = Keyword.query.filter_by(name=new_kw_name).first()
            if not target_kw:
                target_kw = Keyword(name=new_kw_name)
                db.session.add(target_kw)
                db.session.flush()

        if target_kw:
            if target_kw not in dataset.keywords:
                dataset.keywords.append(target_kw)
                db.session.commit()
                flash(f'Successfully attached keyword "#{target_kw.name}" to dataset.', 'success')
            else:
                flash(f'Dataset already has keyword "#{target_kw.name}".', 'info')
        else:
            flash('Please specify a valid keyword term.', 'error')

    elif action == 'remove':
        kw_id = request.form.get('keyword_id')
        if kw_id and kw_id.isdigit():
            kw = db.session.get(Keyword, int(kw_id))
            if kw and kw in dataset.keywords:
                dataset.keywords.remove(kw)
                db.session.commit()
                flash(f'Detached keyword "#{kw.name}" from dataset.', 'info')

    if redirect_to == 'admin_keywords':
        return redirect(url_for('admin_keywords'))
    return redirect(url_for('detail', dataset_id=dataset.id))


@app.route('/admin/keywords', methods=['GET', 'POST'])
@login_required
def admin_keywords():
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'cleanup_orphan':
            orphans = Keyword.query.filter(~Keyword.datasets.any()).all()
            count = len(orphans)
            for kw in orphans:
                db.session.delete(kw)
            db.session.commit()
            flash(f'Cleaned {count} orphan keywords successfully.', 'success')
            return redirect(url_for('admin_keywords'))

    datasets = Dataset.query.order_by(Dataset.created_at.desc()).all()
    all_keywords = Keyword.query.order_by(Keyword.name.asc()).all()
    return render_template('admin/keywords.html', datasets=datasets, all_keywords=all_keywords)

    datasets = Dataset.query.order_by(Dataset.created_at.desc()).all()
    all_keywords = Keyword.query.order_by(Keyword.name.asc()).all()
    return render_template('admin/keywords.html', datasets=datasets, all_keywords=all_keywords)

# ----------------- Admin Cloud Storage (OSS/MinIO) Configuration -----------------
@app.route('/admin/oss', methods=['GET', 'POST'])
@login_required
def admin_oss():
    if current_user.role != 'admin':
        flash('Access denied. Administrator privileges required.', 'error')
        return redirect(url_for('index'))

    oss_config = StorageConfig.query.first()

    if request.method == 'POST':
        provider = request.form.get('provider', 'aliyun_oss').strip()
        endpoint = request.form.get('endpoint', '').strip().replace('https://', '').replace('http://', '')
        bucket_name = request.form.get('bucket_name', '').strip()
        access_key_id = request.form.get('access_key_id', '').strip()
        access_key_secret = request.form.get('access_key_secret', '').strip()
        custom_domain = request.form.get('custom_domain', '').strip()
        is_active = bool(request.form.get('is_active'))

        if not endpoint or not bucket_name or not access_key_id or not access_key_secret:
            flash('Please complete all required storage credentials.', 'error')
            return redirect(url_for('admin_oss'))

        if not oss_config:
            oss_config = StorageConfig(
                provider=provider,
                endpoint=endpoint,
                bucket_name=bucket_name,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                custom_domain=custom_domain,
                is_active=is_active
            )
            db.session.add(oss_config)
        else:
            oss_config.provider = provider
            oss_config.endpoint = endpoint
            oss_config.bucket_name = bucket_name
            oss_config.access_key_id = access_key_id
            oss_config.access_key_secret = access_key_secret
            oss_config.custom_domain = custom_domain
            oss_config.is_active = is_active

        db.session.commit()
        flash('Storage configuration successfully updated and active.', 'success')
        return redirect(url_for('admin_oss'))

    return render_template('admin/oss.html', oss_config=oss_config)

# ----------------- Admin Dataset Management (CRUD) -----------------
@app.route('/admin/datasets')
@login_required
def admin_datasets():
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    datasets = Dataset.query.order_by(Dataset.created_at.desc()).all()
    return render_template('admin/datasets.html', datasets=datasets)

@app.route('/admin/dataset/<int:dataset_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_dataset(dataset_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    dataset = Dataset.query.get_or_404(dataset_id)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        author = request.form.get('author', '').strip()
        year = request.form.get('year', '').strip()
        site_id = request.form.get('site_id', type=int)
        site_code = request.form.get('site_code', '').strip()
        spatial_scale = request.form.get('spatial_scale', 'Point').strip()
        start_date_str = request.form.get('start_date', '').strip()
        end_date_str = request.form.get('end_date', '').strip()
        temporal_resolution = request.form.get('temporal_resolution', 'Daily').strip()
        data_type = request.form.get('data_type', 'Field Observation').strip()
        scenario = request.form.get('scenario', 'Control').strip()
        min_depth = request.form.get('min_depth', type=float) or 0.0
        max_depth = request.form.get('max_depth', type=float) or 1.0
        doi = request.form.get('doi', '').strip()
        data_source_type = request.form.get('data_source_type', 'github').strip()
        github_url = request.form.get('github_url', '').strip()
        description = request.form.get('description', '').strip()

        if not title or not author or not description:
            flash('Title, author, and description are required.', 'error')
            return redirect(url_for('admin_edit_dataset', dataset_id=dataset.id))

        from datetime import date
        s_date = None
        e_date = None
        try:
            if start_date_str:
                s_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            if end_date_str:
                e_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except Exception:
            pass

        yr_val = int(year) if year.isdigit() else (s_date.year if s_date else dataset.year)

        target_site = None
        if site_id:
            target_site = db.session.get(SiteLocation, site_id)
        elif site_code:
            target_site = SiteLocation.query.filter_by(site_code=site_code).first()

        # Update dataset attributes
        dataset.title = title
        dataset.author = author
        dataset.year = yr_val
        dataset.site = target_site
        dataset.site_code = target_site.site_code if target_site else (site_code or dataset.site_code)
        dataset.spatial_scale = spatial_scale
        if s_date:
            dataset.start_date = s_date
        if e_date:
            dataset.end_date = e_date
        dataset.temporal_resolution = temporal_resolution
        dataset.data_type = data_type
        dataset.scenario = scenario
        dataset.min_depth = min_depth
        dataset.max_depth = max_depth
        dataset.doi = doi
        dataset.data_source_type = data_source_type
        dataset.github_url = github_url if data_source_type == 'github' else None
        dataset.description = description

        # Update variable associations
        var_ids = request.form.getlist('variables')
        dataset.variables.clear()
        for vid in var_ids:
            if str(vid).isdigit():
                v_obj = db.session.get(VariableDefinition, int(vid))
                if v_obj and v_obj not in dataset.variables:
                    dataset.variables.append(v_obj)

        new_custom_vars = request.form.get('custom_variables', '').strip()
        if new_custom_vars:
            raw_v_items = [v.strip() for v in new_custom_vars.replace(',', ' ').split() if v.strip()]
            for item in raw_v_items:
                parts = item.split(':')
                v_code = parts[0].strip().upper()
                v_unit = parts[1].strip() if len(parts) > 1 else 'dimensionless'
                v_obj = VariableDefinition.query.filter_by(code=v_code).first()
                if not v_obj:
                    v_obj = VariableDefinition(
                        code=v_code,
                        display_name=f"{v_code} (User Added)",
                        display_name_en=f"{v_code} (User Added)",
                        category='Microbial Pool',
                        unit=v_unit,
                        description='User-defined variable',
                        description_en='User-defined variable'
                    )
                    db.session.add(v_obj)
                    db.session.flush()
                if v_obj not in dataset.variables:
                    dataset.variables.append(v_obj)

        # Update keyword associations
        kw_ids = request.form.getlist('keyword_ids')
        dataset.keywords.clear()
        for kid in kw_ids:
            if str(kid).isdigit():
                k_obj = db.session.get(Keyword, int(kid))
                if k_obj:
                    dataset.keywords.append(k_obj)

        extra_kws = request.form.get('keywords', '').strip()
        if extra_kws:
            raw_kws = [k.strip().lower() for k in extra_kws.replace(',', ' ').split() if k.strip()]
            for k_name in raw_kws:
                kw_obj = Keyword.query.filter_by(name=k_name).first()
                if not kw_obj:
                    kw_obj = Keyword(name=k_name)
                    db.session.add(kw_obj)
                    db.session.flush()
                if kw_obj not in dataset.keywords:
                    dataset.keywords.append(kw_obj)

        db.session.commit()
        flash(f'Dataset #{dataset.id} updated successfully.', 'success')
        return redirect(url_for('admin_datasets'))

    all_sites = SiteLocation.query.order_by(SiteLocation.site_code.asc()).all()
    all_variables = VariableDefinition.query.order_by(VariableDefinition.category.asc()).all()
    all_keywords = Keyword.query.order_by(Keyword.name.asc()).all()
    return render_template('admin/dataset_edit.html', dataset=dataset, all_sites=all_sites, all_variables=all_variables, all_keywords=all_keywords)

@app.route('/admin/dataset/<int:dataset_id>/delete', methods=['POST'])
@login_required
def admin_delete_dataset(dataset_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    dataset = Dataset.query.get_or_404(dataset_id)
    ds_title = dataset.title

    DownloadLog.query.filter_by(dataset_id=dataset.id).delete()
    dataset.keywords.clear()
    dataset.variables.clear()

    db.session.delete(dataset)
    db.session.commit()

    flash(f'Dataset #{dataset_id} "{ds_title[:20]}..." was permanently deleted.', 'info')
    return redirect(url_for('admin_datasets'))

# ----------------- Admin Standardized Variables Dictionary (CRUD) -----------------
@app.route('/admin/variables', methods=['GET', 'POST'])
@login_required
def admin_variables():
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    edit_id = request.args.get('edit_id', type=int)
    edit_var = db.session.get(VariableDefinition, edit_id) if edit_id else None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'create':
            code = request.form.get('code', '').strip().upper()
            display_name = request.form.get('display_name', '').strip()
            display_name_en = request.form.get('display_name_en', '').strip()
            category = request.form.get('category', 'Gas Flux').strip()
            unit = request.form.get('unit', '').strip()
            description = request.form.get('description', '').strip()
            description_en = request.form.get('description_en', '').strip()

            if not code or not display_name or not unit:
                flash('Variable code, display name, and physical unit are required.', 'error')
                return redirect(url_for('admin_variables'))

            if VariableDefinition.query.filter_by(code=code).first():
                flash(f'Variable code "{code}" already exists.', 'error')
                return redirect(url_for('admin_variables'))

            new_v = VariableDefinition(
                code=code,
                display_name=display_name,
                display_name_en=display_name_en,
                category=category,
                unit=unit,
                description=description,
                description_en=description_en
            )
            db.session.add(new_v)
            db.session.commit()
            flash(f'Scientific variable "{code}" published successfully.', 'success')
            return redirect(url_for('admin_variables'))

        elif action == 'update':
            var_id = request.form.get('var_id', type=int)
            v_obj = db.session.get(VariableDefinition, var_id)
            if v_obj:
                v_obj.display_name = request.form.get('display_name', '').strip()
                v_obj.display_name_en = request.form.get('display_name_en', '').strip()
                v_obj.category = request.form.get('category', '').strip()
                v_obj.unit = request.form.get('unit', '').strip()
                v_obj.description = request.form.get('description', '').strip()
                v_obj.description_en = request.form.get('description_en', '').strip()
                db.session.commit()
                flash(f'Variable "{v_obj.code}" updated successfully.', 'success')
            return redirect(url_for('admin_variables'))

    variables = VariableDefinition.query.order_by(VariableDefinition.category.asc(), VariableDefinition.code.asc()).all()
    return render_template('admin/variables.html', variables=variables, edit_var=edit_var)

@app.route('/admin/variable/<int:var_id>/delete', methods=['POST'])
@login_required
def admin_delete_variable(var_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('index'))

    v_obj = db.session.get(VariableDefinition, var_id)
    if v_obj:
        v_code = v_obj.code
        db.session.delete(v_obj)
        db.session.commit()
        flash(f'Variable "{v_code}" deleted from dictionary.', 'info')

    return redirect(url_for('admin_variables'))

import os

if __name__ == '__main__':
    seed_database()
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
