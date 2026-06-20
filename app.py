import os
import time
from datetime import datetime
from dotenv import load_dotenv
import unicodedata
load_dotenv()

import csv
import io
import re

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, make_response

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
# Primeiro o app nasce
app = Flask(__name__)

# configura o cookie
app.config['SESSION_COOKIE_NAME'] = '__session'
app.secret_key = os.environ.get('SECRET_KEY')

# Configuração da versão atual
app.config['VERSION'] = '1.1.0'

# --- TRAVA DE SEGURANÇA GLOBAL DO ADMIN ---
ADMIN_PANEL_ENABLED = os.environ.get('ENABLE_ADMIN_PANEL', 'False') == 'True'

@app.context_processor
def inject_globals():
    return dict(versao_site=app.config['VERSION'], admin_panel_enabled=ADMIN_PANEL_ENABLED)

@app.template_filter('brl')
def brl_filter(value):
    try:
        return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return "0,00"

# --- MODO DE MANUTENÇÃO ---
MODO_MANUTENCAO = False

@app.before_request
def verificar_manutencao():
    if MODO_MANUTENCAO and request.endpoint != 'static':
        from flask import render_template
        return render_template('manutencao.html'), 503

# --- CHAVES DE ACESSO DO OPEN FINANCE - API ---
PLUGGY_CLIENT_ID = os.environ.get('PLUGGY_CLIENT_ID')
PLUGGY_CLIENT_SECRET = os.environ.get('PLUGGY_CLIENT_SECRET')

# --- CHAVE DO BANCO DE DADOS NA NUVEM (NEON/POSTGRESQL) ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def format_brl(value):
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY, 
            nome TEXT, 
            email TEXT UNIQUE, 
            senha TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS transacoes (
            id SERIAL PRIMARY KEY, 
            usuario_id INTEGER REFERENCES usuarios(id), 
            descricao TEXT, 
            valor REAL, 
            tipo TEXT, 
            categoria TEXT, 
            data TEXT,
            banco TEXT DEFAULT 'Manual'
        )
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cartoes (
            id SERIAL PRIMARY KEY,
            usuario_id INTEGER REFERENCES usuarios(id),
            nome TEXT,
            limite REAL,
            dia_fechamento INTEGER,
            dia_vencimento INTEGER
        )
    ''')
    
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='transacoes' AND column_name='cartao_id'")
    if not cur.fetchone():
        cur.execute('ALTER TABLE transacoes ADD COLUMN cartao_id INTEGER REFERENCES cartoes(id)')
        
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='transacoes' AND column_name='tags'")
    if not cur.fetchone():
        cur.execute('ALTER TABLE transacoes ADD COLUMN tags TEXT')

    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='transacoes' AND column_name='observacao'")
    if not cur.fetchone():
        cur.execute('ALTER TABLE transacoes ADD COLUMN observacao TEXT')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS metas (
            id SERIAL PRIMARY KEY,
            usuario_id INTEGER REFERENCES usuarios(id),
            categoria TEXT,
            limite REAL,
            mes INTEGER,
            ano INTEGER,
            UNIQUE(usuario_id, categoria, mes, ano)
        )
    ''')

    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='usuarios' AND column_name='is_admin'")
    if not cur.fetchone():
        cur.execute('ALTER TABLE usuarios ADD COLUMN is_admin BOOLEAN DEFAULT FALSE')
        cur.execute('UPDATE usuarios SET is_admin = TRUE WHERE id = 1')

    conn.commit()
    cur.close()
    conn.close()

# --- MOTOR INTELIGENTE DE CATEGORIZAÇÃO (V3.0) ---
def categorizar_automaticamente(descricao, categoria_pluggy='Outros', tipo_transacao='Despesa'):
    desc = str(descricao).lower()
    try:
        desc = ''.join(c for c in unicodedata.normalize('NFD', desc) if unicodedata.category(c) != 'Mn')
    except:
        pass 

    prefixos_limpar = ['mp*', 'mp *', 'pg*', 'pg *', 'pg*ton ', 'zp*', 'zp *', 'payu*', 'pag*']
    for prefixo in prefixos_limpar:
        if desc.startswith(prefixo):
            desc = desc.replace(prefixo, '', 1).strip()

    if tipo_transacao.lower() in ['receita', 'entrada']:
        dicionario_receitas = {
            'Salário e Receitas': ['salario', 'adiantamento', 'honorarios', 'rendimento', 'proventos', 'restituicao'],
            'Transferência Recebida': ['pix recebido', 'ted recebida', 'doc recebido', 'transferencia recebida', 'pix'],
            'Cashback e Bônus': ['cashback', 'bonus', 'reembolso']
        }
        for categoria, palavras in dicionario_receitas.items():
            if any(palavra in desc for palavra in palavras):
                return categoria
        return 'Outras Receitas'

    dicionario_despesas = {
        'Pagamentos e Boletos': ['pagamento de boleto', 'pagamento de titulo', 'boleto', 'conta', 'imposto', 'darf', 'ipva', 'iptu'],
        'Alimentação': ['ifood', 'rappi', 'mcdonalds', 'burger king', 'bk', 'mercado', 'supermercado', 'atacadao', 'carrefour', 'padaria', 'restaurante', 'pizzaria', 'assai', 'zaffari', 'ze delivery', 'outback', 'habibs', 'coco bambu', 'pao de acucar', 'extra', 'hortifruti', 'swift', 'ambev', 'cacau show', 'kfc', 'subway', 'bar ', 'botequim', 'cafe', 'lanchonete', 'confeitaria', 'sorveteria', 'doceria'],
        'Transporte': ['uber', '99', '99app', 'posto', 'ipiranga', 'shell', 'petrobras', 'ale', 'estacionamento', 'pedagio', 'sem parar', 'veloe', 'conectcar', 'metro', 'cptm', 'passagem', 'latam', 'gol', 'azul', 'decolar', '123milhas', 'localiza', 'movida', 'unidas', 'bilhete unico', 'clickbus', 'buser', 'viacao', 'balsa'],
        'Moradia': ['enel', 'sabesp', 'copel', 'cemig', 'light', 'sanepar', 'compesa', 'ceb', 'caesb', 'condominio', 'aluguel', 'internet', 'vivo', 'claro', 'tim', 'oi', 'net ', 'sky', 'leroy merlin', 'telhanorte', 'c&c', 'tok stok', 'mobly', 'imobiliaria'],
        'Saúde': ['farmacia', 'drogasil', 'raia', 'pague menos', 'pacheco', 'sao paulo', 'unimed', 'amil', 'sulamerica', 'bradesco saude', 'hapvida', 'hospital', 'clinica', 'odontoprev', 'sorridentes', 'exame', 'laboratorio', 'fleury', 'sabin', 'terapia', 'psicologo'],
        'Lazer e Assinaturas': ['netflix', 'spotify', 'amazon prime', 'prime video', 'hbo', 'disney', 'star+', 'globoplay', 'cinema', 'ingresso', 'cinemark', 'cinepolis', 'kinoplex', 'sympla', 'eventim', 'steam', 'playstation', 'xbox', 'nintendo', 'itunes', 'google play', 'tinder', 'deezer', 'twitch', 'apple.com/bill', 'riot games', 'smart fit', 'smartfit', 'academia'],
        'Educação': ['escola', 'faculdade', 'universidade', 'curso', 'alura', 'udemy', 'ingles', 'idiomas', 'estacio', 'anhanguera', 'puc', 'mackenzie', 'fgv', 'livraria', 'saraiva', 'leitura'],
        'Compras e Lojas': ['amazon', 'mercadolivre', 'mercado livre', 'shopee', 'shein', 'aliexpress', 'renner', 'riachuelo', 'c&a', 'cea', 'zara', 'centauro', 'netshoes', 'dafiti', 'magalu', 'casas bahia', 'ponto frio', 'americanas', 'havan', 'boticario', 'natura', 'sephora', 'privalia'],
        'Pets': ['cobasi', 'petz', 'pet shop', 'veterinario', 'racao'],
        'Taxas e Serviços': ['tarifa', 'anuidade', 'iof', 'juros', 'multa', 'seguro', 'correios', 'loggi', 'mensalidade', 'cartorio', 'despachante', 'hotmart'],
        'Transferência Enviada': ['transferencia', 'pix enviado', 'ted enviada', 'doc enviado']
    }

    for categoria, palavras in dicionario_despesas.items():
        if any(palavra in desc for palavra in palavras):
            return categoria
            
    traducoes_pluggy = {
        'Food and Drink': 'Alimentação', 'Travel': 'Transporte', 'Health': 'Saúde',
        'Payment': 'Pagamentos e Boletos', 'Transfers': 'Transferência Enviada',
        'Shopping': 'Compras e Lojas', 'Personal Care': 'Saúde'
    }
    return traducoes_pluggy.get(categoria_pluggy, 'Outras Despesas')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower() 
        senha = request.form.get('senha', '')
        confirmar_senha = request.form.get('confirmar_senha', '')

        if senha != confirmar_senha:
            flash('Erro: As senhas não coincidem.', 'error')
            return render_template('cadastro.html')

        padrao_senha = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$"
        if not re.match(padrao_senha, senha):
            flash('Erro: Senha fraca. Siga as regras de segurança.', 'error')
            return render_template('cadastro.html')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO usuarios (nome, email, senha) VALUES (%s, %s, %s)', (nome, email, senha))
            conn.commit()
            flash('Conta criada com sucesso! Você já pode acessar.', 'success')
            return redirect(url_for('login'))
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('Erro: Este e-mail já está em uso.', 'error')
        finally: 
            cur.close()
            conn.close()
            
    return render_template('cadastro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, senha = request.form['email'], request.form['senha']
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM usuarios WHERE email = %s AND senha = %s', (email, senha))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user:
            session['user_id'] = user['id']
            session['user_nome'] = user['nome']
            session['is_admin'] = user.get('is_admin', False) 
            
            # Bifurcação segura
            if session['is_admin'] and ADMIN_PANEL_ENABLED:
                return redirect(url_for('admin'))
            else:
                return redirect(url_for('dashboard'))
                
        flash('E-mail ou senha incorretos.', 'error')
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    if session.get('is_admin') and ADMIN_PANEL_ENABLED: return redirect(url_for('admin'))
    
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('SELECT * FROM usuarios WHERE id = %s', (user_id,))
    user_data = cur.fetchone()

    filtro_tipo = request.args.get('filtro_tipo', 'Todos')
    filtro_categoria = request.args.get('filtro_categoria', 'Todas')
    filtro_banco = request.args.get('filtro_banco', 'Todos')

    # --- LÓGICA DE PAGINAÇÃO (DASHBOARD) ---
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    query_base = " FROM transacoes WHERE usuario_id = %s"
    params = [user_id]

    if filtro_tipo != 'Todos':
        query_base += " AND LOWER(tipo) = LOWER(%s)"
        params.append(filtro_tipo)
    if filtro_categoria != 'Todas':
        query_base += " AND categoria = %s"
        params.append(filtro_categoria)
    if filtro_banco != 'Todos':
        query_base += " AND banco = %s"
        params.append(filtro_banco)

    cur.execute("SELECT COUNT(*)" + query_base, tuple(params))
    total_items = cur.fetchone()['count']
    total_pages = (total_items + per_page - 1) // per_page
    if total_pages == 0: total_pages = 1

    query_final = "SELECT *" + query_base + " ORDER BY data DESC LIMIT %s OFFSET %s"
    params_final = params.copy()
    params_final.extend([per_page, offset])
    
    cur.execute(query_final, tuple(params_final))
    transacoes = cur.fetchall()
    # ---------------------------------------

    cur.execute('SELECT DISTINCT banco FROM transacoes WHERE usuario_id = %s', (user_id,))
    db_bancos = cur.fetchall()
    all_bancos = [row['banco'] if row['banco'] else 'Manual' for row in db_bancos]
    all_bancos = list(set(all_bancos))
    all_bancos.sort()

    receitas = 0
    despesas = 0

    cur.execute('SELECT * FROM transacoes WHERE usuario_id = %s', (user_id,))
    todas_transacoes = cur.fetchall()
    
    for t in todas_transacoes:
        tipo = str(t['tipo']).strip().title() 
        if tipo in ['Receita', 'Entrada']:
            receitas += t['valor']
        elif tipo in ['Despesa', 'Saída', 'Saida']:
            despesas += t['valor']

    ent = receitas
    sai = despesas

    cur.execute('''
        SELECT categoria, ABS(SUM(valor)) as total FROM transacoes 
        WHERE usuario_id = %s AND LOWER(TRIM(tipo)) IN ('despesa', 'saída', 'saida')
        GROUP BY categoria
    ''', (user_id,))
    dados_grafico = cur.fetchall()

    labels_cat = [d['categoria'] for d in dados_grafico]
    valores_cat = [float(d['total']) for d in dados_grafico]
    
    default_cats = ['Alimentação', 'Moradia', 'Transporte', 'Lazer', 'Saúde', 'Salário', 'Outros']
    cur.execute('SELECT DISTINCT categoria FROM transacoes WHERE usuario_id = %s', (user_id,))
    db_cats = cur.fetchall()
    user_cats = [row['categoria'] for row in db_cats if row['categoria']]
    all_cats = list(set(default_cats + user_cats))
    all_cats.sort()
    
    info = {
        "nome": session.get('user_nome', 'Usuário'), 
        "saldo": format_brl(ent-sai), 
        "entradas": format_brl(ent), 
        "saidas": format_brl(sai),
        "is_admin": user_data.get('is_admin', False)
    }
   
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', 
                           info=info,
                           transacoes=transacoes, 
                           ent=ent, 
                           sai=sai, 
                           saldo=ent-sai, 
                           labels_cat=labels_cat, 
                           valores_cat=valores_cat, 
                           all_cats=all_cats,
                           filtro_tipo=filtro_tipo,
                           filtro_categoria=filtro_categoria,  
                           filtro_banco=filtro_banco,          
                           all_bancos=all_bancos,
                           page=page,
                           total_pages=total_pages)              
    
@app.route('/importar_csv', methods=['POST'])
def importar_csv():
    return redirect(url_for('dashboard'))

@app.route('/add_transacao', methods=['POST'])
def add_transacao():
    if 'user_id' not in session: return redirect(url_for('login'))
    
    # 1. Pega os dados do formulário
    descricao_base = request.form['descricao'].strip()
    valor_total = abs(float(request.form['valor']))
    tipo = request.form['tipo']
    categoria = request.form['categoria'].strip()
    data_inicial = datetime.strptime(request.form['data'], '%Y-%m-%d')
    parcelas = int(request.form.get('parcelas', 1))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 2. Divide o valor total pela quantidade de parcelas
    valor_parcela = valor_total / parcelas
    
    # 3. Função ajudante para calcular os meses futuros corretamente
    def add_months(current_date, months_to_add):
        month = current_date.month - 1 + months_to_add
        year = current_date.year + month // 12
        month = month % 12 + 1
        day = min(current_date.day, calendar.monthrange(year, month)[1])
        return current_date.replace(year=year, month=month, day=day)

    # 4. Cria as transações no banco (Loop)
    for i in range(parcelas):
        data_parcela = add_months(data_inicial, i)
        
        # Se for parcelado, adiciona a tag (1/3), (2/3)... no final do nome
        if parcelas > 1:
            desc_parcela = f"{descricao_base} ({i+1}/{parcelas})"
        else:
            desc_parcela = descricao_base
            
        cur.execute('''
            INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data, banco) 
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        ''', (session['user_id'], desc_parcela, valor_parcela, tipo, categoria, data_parcela.strftime('%Y-%m-%d'), 'Manual'))
    
    conn.commit()
    cur.close()
    conn.close()
    flash(f'Lançamento salvo! ({parcelas}x)' if parcelas > 1 else 'Transação salva!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/edit_transacao', methods=['POST'])
def edit_transacao():
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    id_transacao = request.form['id']
    valor_limpo = abs(float(request.form['valor']))
    
    cur.execute('''
        UPDATE transacoes 
        SET descricao = %s, valor = %s, tipo = %s, categoria = %s, data = %s
        WHERE id = %s AND usuario_id = %s
    ''', (request.form['descricao'], valor_limpo, request.form['tipo'], 
          request.form['categoria'].strip(), request.form['data'], id_transacao, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Transação atualizada com sucesso!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/delete_transacao/<int:id>')
def delete_transacao(id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM transacoes WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Transação excluída com sucesso.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/clear_transacoes', methods=['POST'])
def clear_transacoes():
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM transacoes WHERE usuario_id = %s', (session['user_id'],))
    conn.commit()
    cur.close()
    conn.close()
    flash('Todos os dados foram apagados!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/gerar_token_pluggy')
def gerar_token_pluggy():
    if 'user_id' not in session: 
        return {"erro": "Usuário não logado"}, 401
        
    try:
        auth_req = requests.post(
            "https://api.pluggy.ai/auth",
            json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET}
        )
        api_key = auth_req.json().get("apiKey")
        
        token_req = requests.post(
            "https://api.pluggy.ai/connect_token",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"options": {"clientUserId": str(session.get('user_id', 'teste'))}} 
        )
        
        return {"token": token_req.json().get("accessToken")}
    except Exception as e:
        return {"erro": str(e)}, 500

@app.route('/sincronizar_pluggy', methods=['POST'])
def sincronizar_pluggy():
    if 'user_id' not in session:
        return {"erro": "Usuário não logado"}, 401

    dados = request.get_json()
    item_id = dados.get('item_id')

    if not item_id:
        return {"erro": "ID do Item não fornecido"}, 400

    try:
        auth_req = requests.post(
            "https://api.pluggy.ai/auth",
            json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET}
        )
        api_key = auth_req.json().get("apiKey")
        headers = {"X-API-KEY": api_key}

        item_req = requests.get(f"https://api.pluggy.ai/items/{item_id}", headers=headers)
        nome_banco = item_req.json().get('connector', {}).get('name', 'Pluggy Bank')

        time.sleep(3)

        contas_req = requests.get(f"https://api.pluggy.ai/accounts?itemId={item_id}", headers=headers)
        contas = contas_req.json().get('results', [])

        conn = get_db_connection()
        cur = conn.cursor()
        transacoes_importadas = 0

        for conta in contas:
            conta_id = conta['id']
            tipo_conta = conta.get('type')
            
            cartao_db_id = None
            
            if tipo_conta == 'CREDIT':
                nome_cartao = f"{nome_banco} {conta.get('name', 'Cartão')}"
                credit_data = conta.get('creditData') or {}
                limite = credit_data.get('creditLimit') or 0
                
                cur.execute('SELECT id FROM cartoes WHERE usuario_id = %s AND nome = %s', (session['user_id'], nome_cartao))
                cartao_existente = cur.fetchone()
                
                if cartao_existente:
                    cartao_db_id = cartao_existente['id']
                    cur.execute('UPDATE cartoes SET limite = %s WHERE id = %s', (limite, cartao_db_id))
                else:
                    cur.execute('''
                        INSERT INTO cartoes (usuario_id, nome, limite, dia_fechamento, dia_vencimento)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    ''', (session['user_id'], nome_cartao, limite, 10, 15))
                    resultado_insert = cur.fetchone()
                    if resultado_insert:
                        cartao_db_id = resultado_insert['id']

            transacoes_req = requests.get(f"https://api.pluggy.ai/transactions?accountId={conta_id}", headers=headers)
            transacoes = transacoes_req.json().get('results', [])

            for t in transacoes:
                descricao = t.get('description', 'Transação Automática')
                
                valor_bruto = t.get('amount')
                if valor_bruto is None: 
                    valor_bruto = 0.0
                    
                valor = abs(valor_bruto)
                tipo = 'Despesa' if valor_bruto < 0 else 'Receita'
                
                categoria_original_pluggy = t.get('category', 'Outros')
                categoria = categorizar_automaticamente(descricao, categoria_original_pluggy, tipo)
                data_transacao = t.get('date', '')[:10]

                cur.execute('''
                    SELECT id FROM transacoes 
                    WHERE usuario_id = %s AND descricao = %s AND valor = %s AND data = %s
                ''', (session['user_id'], descricao, valor, data_transacao))
                
                if cur.fetchone():
                    continue 

                cur.execute('''
                    INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data, banco, cartao_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (session['user_id'], descricao, valor, tipo, categoria, data_transacao, nome_banco, cartao_db_id))
                transacoes_importadas += 1

        conn.commit()
        cur.close()
        conn.close()

        return {"sucesso": True, "total": transacoes_importadas}

    except Exception as e:
        print(f"Erro na sincronização: {e}")
        return {"erro": str(e)}, 500
    
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/cartoes')
def cartoes():
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('SELECT * FROM usuarios WHERE id = %s', (user_id,))
    info = cur.fetchone()

    cur.execute('''
        SELECT 
            banco,
            SUM(CASE WHEN LOWER(TRIM(tipo)) IN ('receita', 'entrada') THEN valor ELSE 0 END) as receitas,
            SUM(CASE WHEN LOWER(TRIM(tipo)) IN ('despesa', 'saída', 'saida') THEN valor ELSE 0 END) as despesas
        FROM transacoes 
        WHERE usuario_id = %s 
        GROUP BY banco
    ''', (user_id,))
    bancos_db = cur.fetchall()

    cur.execute('SELECT * FROM cartoes WHERE usuario_id = %s ORDER BY id DESC', (user_id,))
    meus_cartoes = cur.fetchall()

    cur.close()
    conn.close()

    lista_bancos = []
    for b in bancos_db:
        nome_banco = b['banco'] if b['banco'] else 'Manual'
        rec = float(b['receitas'] or 0)
        des = float(b['despesas'] or 0)
        saldo = rec - des
        nome_imagem = nome_banco.lower().replace(' ', '_') + '.png'
        lista_bancos.append({'nome': nome_banco, 'saldo': saldo, 'receitas': rec, 'despesas': des, 'imagem': nome_imagem})

    return render_template('cartoes.html', info=info, bancos=lista_bancos, cartoes=meus_cartoes)

@app.route('/add_cartao', methods=['POST'])
def add_cartao():
    if 'user_id' in session:
        nome = request.form['nome']
        limite = float(request.form['limite'])
        dia_fechamento = int(request.form['dia_fechamento'])
        dia_vencimento = int(request.form['dia_vencimento'])
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO cartoes (usuario_id, nome, limite, dia_fechamento, dia_vencimento)
            VALUES (%s, %s, %s, %s, %s)
        ''', (session['user_id'], nome, limite, dia_fechamento, dia_vencimento))
        conn.commit()
        cur.close()
        conn.close()
        flash('Cartão adicionado com sucesso!', 'success')
    return redirect(url_for('cartoes'))

@app.route('/delete_cartao/<int:id>')
def delete_cartao(id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE transacoes SET cartao_id = NULL WHERE cartao_id = %s AND usuario_id = %s', (id, session['user_id']))
    cur.execute('DELETE FROM cartoes WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Cartão excluído com sucesso.', 'success')
    return redirect(url_for('cartoes'))

@app.route('/fatura/<int:cartao_id>')
def fatura(cartao_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT * FROM cartoes WHERE id = %s AND usuario_id = %s', (cartao_id, session['user_id']))
    cartao = cur.fetchone()
    
    if not cartao:
        flash('Cartão não encontrado.', 'error')
        return redirect(url_for('cartoes'))

    hoje = datetime.now()
    mes_atual = int(request.args.get('mes', hoje.month))
    ano_atual = int(request.args.get('ano', hoje.year))

    mes_passado = mes_atual - 1 if mes_atual > 1 else 12
    ano_passado = ano_atual if mes_atual > 1 else ano_atual - 1
    mes_proximo = mes_atual + 1 if mes_atual < 12 else 1
    ano_proximo = ano_atual if mes_atual < 12 else ano_atual + 1

    meses_pt = {1: 'Jan', 2: 'Fev', 3: 'Mar', 4: 'Abr', 5: 'Mai', 6: 'Jun', 7: 'Jul', 8: 'Ago', 9: 'Set', 10: 'Out', 11: 'Nov', 12: 'Dez'}
    nome_mes_atual = f"{meses_pt[mes_atual]} {ano_atual}"

    busca = request.args.get('busca', '').strip()
    filtro_tipo = request.args.get('filtro_tipo', 'Todos')
    filtro_categoria = request.args.get('filtro_categoria', 'Todas')
    filtro_tag = request.args.get('filtro_tag', 'Todas')

    # --- LÓGICA DE PAGINAÇÃO E TOTAIS (FATURA) ---
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    query_base = " FROM transacoes WHERE cartao_id = %s AND usuario_id = %s AND data LIKE %s"
    params = [cartao_id, session['user_id'], f"{ano_atual}-{mes_atual:02d}-%"]

    if busca:
        query_base += " AND descricao ILIKE %s"
        params.append(f"%{busca}%")
    if filtro_tipo != 'Todos':
        query_base += " AND LOWER(TRIM(tipo)) = LOWER(%s)"
        params.append(filtro_tipo)
    if filtro_categoria != 'Todas':
        query_base += " AND categoria = %s"
        params.append(filtro_categoria)
    if filtro_tag != 'Todas':
        query_base += " AND tags ILIKE %s"
        params.append(f"%{filtro_tag}%")

    # 1. Puxa TODAS as filtradas para calcular o Total da Fatura corretamente
    cur.execute("SELECT *" + query_base, tuple(params))
    todas_filtradas = cur.fetchall()
    
    total_items = len(todas_filtradas)
    total_pages = (total_items + per_page - 1) // per_page
    if total_pages == 0: total_pages = 1

    total_fatura = 0.0
    for t in todas_filtradas:
        tipo_t = str(t['tipo']).strip().lower()
        if tipo_t in ['despesa', 'saída', 'saida']: total_fatura += float(t['valor'])
        elif tipo_t in ['receita', 'entrada']: total_fatura -= float(t['valor'])

    # 2. Puxa APENAS as 20 da página atual para a lista
    query_final = "SELECT *" + query_base + " ORDER BY data DESC LIMIT %s OFFSET %s"
    params_final = params.copy()
    params_final.extend([per_page, offset])
    
    cur.execute(query_final, tuple(params_final))
    transacoes = cur.fetchall()
    # ---------------------------------------------

    cur.execute('SELECT DISTINCT categoria FROM transacoes WHERE cartao_id = %s AND usuario_id = %s', (cartao_id, session['user_id']))
    all_cats = sorted([row['categoria'] for row in cur.fetchall() if row['categoria']])

    cur.execute("SELECT DISTINCT tags FROM transacoes WHERE cartao_id = %s AND usuario_id = %s AND tags IS NOT NULL AND tags != ''", (cartao_id, session['user_id']))
    tags_banco = cur.fetchall()
    lista_tags_unicas = set()
    for row in tags_banco:
        for t_item in row['tags'].split(','):
            if t_item.strip():
                lista_tags_unicas.add(t_item.strip())
    all_tags = sorted(list(lista_tags_unicas))

    cur.execute('SELECT valor, tipo FROM transacoes WHERE cartao_id = %s AND usuario_id = %s', (cartao_id, session['user_id']))
    total_real = 0.0
    for t in cur.fetchall():
        tipo_real = str(t['tipo']).strip().lower()
        if tipo_real in ['despesa', 'saída', 'saida']: total_real += float(t['valor'])
        elif tipo_real in ['receita', 'entrada']: total_real -= float(t['valor'])
    limite_disp = float(cartao['limite']) - total_real

    cur.close()
    conn.close()

    return render_template('fatura.html', cartao=cartao, transacoes=transacoes, 
                           total_fatura=total_fatura, limite_disp=limite_disp,
                           busca=busca, filtro_tipo=filtro_tipo, filtro_categoria=filtro_categoria, filtro_tag=filtro_tag,
                           all_cats=all_cats, all_tags=all_tags,
                           mes_passado=mes_passado, ano_passado=ano_passado,
                           mes_proximo=mes_proximo, ano_proximo=ano_proximo,
                           nome_mes_atual=nome_mes_atual, mes_atual=mes_atual, ano_atual=ano_atual,
                           page=page, total_pages=total_pages)

@app.route('/salvar_detalhes_transacao', methods=['POST'])
def salvar_detalhes_transacao():
    if 'user_id' not in session: return redirect(url_for('login'))
    t_id = request.form.get('id')
    cartao_id = request.form.get('cartao_id')
    descricao = request.form.get('descricao')
    data = request.form.get('data')
    categoria = request.form.get('categoria')
    tags = request.form.get('tags', '').strip()
    observacao = request.form.get('observacao', '').strip()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        UPDATE transacoes 
        SET descricao = %s, data = %s, categoria = %s, tags = %s, observacao = %s
        WHERE id = %s AND usuario_id = %s
    ''', (descricao, data, categoria, tags, observacao, t_id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Detalhes da transação atualizados!', 'success')
    return redirect(url_for('fatura', cartao_id=cartao_id))

@app.route('/delete_transacao_fatura/<int:id>/<int:cartao_id>')
def delete_transacao_fatura(id, cartao_id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM transacoes WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Transação removida da fatura.', 'success')
    return redirect(url_for('fatura', cartao_id=cartao_id))

@app.route('/metas')
def metas():
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']
    hoje = datetime.now()
    mes_atual = int(request.args.get('mes', hoje.month))
    ano_atual = int(request.args.get('ano', hoje.year))
    
    mes_passado = mes_atual - 1 if mes_atual > 1 else 12
    ano_passado = ano_atual if mes_atual > 1 else ano_atual - 1
    mes_proximo = mes_atual + 1 if mes_atual < 12 else 1
    ano_proximo = ano_atual if mes_atual < 12 else ano_atual + 1
    
    meses_pt = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    nome_mes_atual = f"{meses_pt[mes_atual]} {ano_atual}"
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT * FROM metas WHERE usuario_id = %s AND mes = %s AND ano = %s', (user_id, mes_atual, ano_atual))
    metas_db = cur.fetchall()
    
    lista_metas = []
    total_disponivel_geral = 0.0
    
    for m in metas_db:
        cur.execute('''
            SELECT * FROM transacoes 
            WHERE usuario_id = %s AND categoria = %s AND data LIKE %s
            ORDER BY data DESC
        ''', (user_id, m['categoria'], f"{ano_atual}-{mes_atual:02d}-%"))
        transacoes_da_meta = cur.fetchall()
        
        gasto = sum(float(t['valor']) for t in transacoes_da_meta if str(t['tipo']).strip().lower() in ['despesa', 'saída', 'saida'])
        disponivel = float(m['limite']) - gasto
        total_disponivel_geral += disponivel
        
        porcentagem = (gasto / float(m['limite'])) * 100 if m['limite'] > 0 else 0
        if porcentagem > 100: porcentagem = 100
        
        cur.execute('''
            SELECT SUM(valor) as total FROM transacoes 
            WHERE usuario_id = %s AND categoria = %s AND data LIKE %s AND LOWER(TRIM(tipo)) IN ('despesa', 'saída', 'saida')
        ''', (user_id, m['categoria'], f"{ano_passado}-{mes_passado:02d}-%"))
        row_ant = cur.fetchone()
        gasto_anterior = float(row_ant['total'] or 0.0) if row_ant else 0.0
        
        diferenca = 0.0
        if gasto_anterior > 0:
            diferenca = ((gasto - gasto_anterior) / gasto_anterior) * 100
            
        cur.execute('SELECT id FROM metas WHERE usuario_id = %s AND categoria = %s AND mes = %s AND ano = %s', 
                    (user_id, m['categoria'], mes_passado, ano_passado))
        tem_anterior = True if cur.fetchone() else False
        
        lista_metas.append({
            'id': m['id'],
            'categoria': m['categoria'],
            'limite': m['limite'],
            'gasto': gasto,
            'disponivel': disponivel,
            'porcentagem': porcentagem,
            'tem_anterior': tem_anterior,
            'diferenca': diferenca,
            'transacoes': transacoes_da_meta
        })
        
    cur.execute('SELECT DISTINCT categoria FROM transacoes WHERE usuario_id = %s', (user_id,))
    all_cats = sorted([row['categoria'] for row in cur.fetchall() if row['categoria']])
    
    cur.close()
    conn.close()
    
    return render_template('metas.html', metas=lista_metas, all_cats=all_cats,
                           total_disponivel=total_disponivel_geral,
                           mes_passado=mes_passado, ano_passado=ano_passado,
                           mes_proximo=mes_proximo, ano_proximo=ano_proximo,
                           nome_mes_atual=nome_mes_atual, mes_atual=mes_atual, ano_atual=ano_atual)

@app.route('/add_meta', methods=['POST'])
def add_meta():
    if 'user_id' not in session: return redirect(url_for('login'))
    categoria = request.form['categoria'].strip()
    limite = float(request.form['limite'])
    mes = int(request.form['mes'])
    ano = int(request.form['ano'])
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT id FROM metas WHERE usuario_id = %s AND categoria = %s AND mes = %s AND ano = %s', 
                (session['user_id'], categoria, mes, ano))
    meta_existente = cur.fetchone()
    
    if meta_existente:
        cur.execute('UPDATE metas SET limite = %s WHERE id = %s', (limite, meta_existente['id']))
    else:
        cur.execute('''
            INSERT INTO metas (usuario_id, categoria, limite, mes, ano)
            VALUES (%s, %s, %s, %s, %s)
        ''', (session['user_id'], categoria, limite, mes, ano))
        
    conn.commit()
    cur.close()
    conn.close()
    
    flash('Limite de gastos configurado!', 'success')
    return redirect(url_for('metas', mes=mes, ano=ano))

@app.route('/delete_meta/<int:id>')
def delete_meta(id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT mes, ano FROM metas WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
    meta = cur.fetchone()
    if meta:
        mes, ano = meta['mes'], meta['ano']
        cur.execute('DELETE FROM metas WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
        conn.commit()
        cur.close()
        conn.close()
        flash('Limite removido.', 'success')
        return redirect(url_for('metas', mes=mes, ano=ano))
    cur.close()
    conn.close()
    return redirect(url_for('metas'))

@app.route('/reclassificar_tudo')
def reclassificar_tudo():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT id, descricao, categoria, tipo FROM transacoes WHERE usuario_id = %s', (session['user_id'],))
    transacoes = cur.fetchall()
    
    atualizadas = 0
    for t in transacoes:
        nova_categoria = categorizar_automaticamente(t['descricao'], t['categoria'], t['tipo'])
        if nova_categoria != t['categoria']:
            cur.execute('UPDATE transacoes SET categoria = %s WHERE id = %s', (nova_categoria, t['id']))
            atualizadas += 1
            
    conn.commit()
    cur.close()
    conn.close()
    
    flash(f'Mágica feita! {atualizadas} transações antigas foram reclassificadas com a nova inteligência.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/admin')
def admin():
    if not ADMIN_PANEL_ENABLED:
        from flask import abort
        abort(404)

    if 'user_id' not in session: return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT is_admin FROM usuarios WHERE id = %s', (session['user_id'],))
    user = cur.fetchone()
    if not user or not user['is_admin']:
        flash('Acesso negado. Área restrita.', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('dashboard'))
        
    cur.execute('SELECT COUNT(*) as total FROM usuarios')
    total_users = cur.fetchone()['total']
    cur.execute('SELECT COUNT(*) as total FROM transacoes')
    total_transacoes = cur.fetchone()['total']
    cur.execute('SELECT COUNT(*) as total FROM cartoes')
    total_cartoes = cur.fetchone()['total']
    
    cur.execute('''
        SELECT descricao, tipo, COUNT(*) as ocorrencias 
        FROM transacoes 
        WHERE categoria ILIKE '%Outr%'
        GROUP BY descricao, tipo
        ORDER BY ocorrencias DESC 
        LIMIT 50
    ''')
    laboratorio = cur.fetchall()

    cur.execute('''
        SELECT u.id, u.nome, u.email, u.is_admin,
               (SELECT COUNT(*) FROM transacoes t WHERE t.usuario_id = u.id) as total_tx,
               (SELECT COUNT(*) FROM cartoes c WHERE c.usuario_id = u.id) as total_cartoes
        FROM usuarios u
        ORDER BY u.id ASC
    ''')
    lista_usuarios = cur.fetchall()

    cur.execute('''
        SELECT 
            CASE WHEN banco = 'Manual' THEN 'Lançamento Manual' ELSE 'API Pluggy' END as origem,
            COUNT(*) as qtd
        FROM transacoes
        GROUP BY CASE WHEN banco = 'Manual' THEN 'Lançamento Manual' ELSE 'API Pluggy' END
    ''')
    origem_dados = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('admin.html', 
                           total_users=total_users, 
                           total_transacoes=total_transacoes, 
                           total_cartoes=total_cartoes, 
                           laboratorio=laboratorio,
                           lista_usuarios=lista_usuarios,
                           origem_dados=origem_dados)

@app.route('/exportar_transacoes')
def exportar_transacoes():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Busca todo o histórico financeiro do usuário
    cur.execute('''
        SELECT data, descricao, categoria, valor, tipo, banco 
        FROM transacoes 
        WHERE usuario_id = %s 
        ORDER BY data DESC
    ''', (session['user_id'],))
    transacoes = cur.fetchall()
    
    cur.close()
    conn.close()
    
    # Criamos o arquivo CSV em memória usando a biblioteca 'io'
    si = io.StringIO()
    # Usamos o delimitador ';' porque o Excel em português reconhece automaticamente como colunas
    cw = csv.writer(si, delimiter=';', lineterminator='\n')
    
    # Escreve o cabeçalho do arquivo
    cw.writerow(['Data', 'Descricao', 'Categoria', 'Valor (R$)', 'Tipo', 'Origem/Banco'])
    
    # Preenche o CSV com os dados
    for t in transacoes:
        cw.writerow([
            t['data'],
            t['descricao'],
            t['categoria'],
            f"{t['valor']:.2f}".replace('.', ','), # Formata o número com vírgula para o Excel BR
            t['tipo'],
            t['banco'] if t['banco'] else 'Manual'
        ])
    
    # Transforma o conteúdo em uma resposta de download (Attachment)
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename=nexpass_extrato_{datetime.now().strftime('%Y%m%d')}.csv"
    output.headers["Content-Type"] = "text/csv; charset=utf-8-sig" # 'utf-8-sig' resolve bugs de acentuação no Excel Windows
    
    return output

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)